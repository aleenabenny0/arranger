/* See include/midi.h. Every rule here has a twin in src/arranger/io.py. */
#include "midi.h"

#include <stdlib.h>
#include <string.h>

const midi_limits MIDI_DEFAULT_LIMITS = {
    /* max_bytes */ 8u * 1024u * 1024u,
    /* max_tracks */ 64u,
    /* max_notes */ 60000u,
    /* max_events */ 600000u,
};

#define SUSTAIN_CONTROLLER 64u

/* --- growable arrays ------------------------------------------------------ */

static midi_status grow(void **items, size_t *cap, size_t count, size_t item_size) {
    size_t next;
    void *grown;
    if (count < *cap) {
        return MIDI_OK;
    }
    next = *cap ? *cap * 2u : 16u;
    if (next > SIZE_MAX / item_size) {
        return MIDI_E_NOMEM;
    }
    grown = realloc(*items, next * item_size);
    if (grown == NULL) {
        return MIDI_E_NOMEM;
    }
    *items = grown;
    *cap = next;
    return MIDI_OK;
}

#define PUSH(array, count, cap, value)                                                          \
    do {                                                                                        \
        midi_status push_status = grow((void **)&(array), &(cap), (count), sizeof(*(array)));   \
        if (push_status != MIDI_OK) {                                                           \
            return push_status;                                                                 \
        }                                                                                       \
        (array)[(count)++] = (value);                                                           \
    } while (0)

/* --- a bounded cursor ----------------------------------------------------- */

typedef struct cursor {
    const uint8_t *data;
    size_t len;
    size_t pos;
} cursor;

static midi_status read_byte(cursor *c, uint8_t *out) {
    if (c->pos >= c->len) {
        return MIDI_E_TRUNCATED;
    }
    *out = c->data[c->pos++];
    return MIDI_OK;
}

static midi_status skip(cursor *c, size_t n) {
    if (n > c->len - c->pos) {
        return MIDI_E_TRUNCATED;
    }
    c->pos += n;
    return MIDI_OK;
}

midi_status midi_read_vlq(const uint8_t *data, size_t len, size_t *pos, uint32_t *value) {
    uint32_t acc = 0;
    int i;
    for (i = 0; i < 4; i++) {
        uint8_t b;
        if (*pos >= len) {
            return MIDI_E_TRUNCATED;
        }
        b = data[(*pos)++];
        acc = (acc << 7) | (uint32_t)(b & 0x7Fu);
        if ((b & 0x80u) == 0) {
            *value = acc;
            return MIDI_OK;
        }
    }
    return MIDI_E_BAD_VLQ;
}

static midi_status read_vlq(cursor *c, uint32_t *value) {
    return midi_read_vlq(c->data, c->len, &c->pos, value);
}

static uint32_t be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static uint16_t be16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

/* --- notes still sounding ------------------------------------------------- */

/*
 * A note-on without a matching note-off yet. The Python reader keeps a list
 * per (channel, pitch) and closes the oldest first; this keeps the same
 * first-in, first-out order with an index-linked list per key in one pool, so
 * a file that never closes its notes costs O(1) per event, not O(n).
 */
typedef struct open_note {
    uint64_t start;
    uint32_t next; /* pool index plus one; zero ends the list */
    uint8_t velocity;
} open_note;

typedef struct open_list {
    uint32_t head; /* pool index plus one; zero when empty */
    uint32_t tail;
} open_list;

typedef struct open_notes {
    open_note *pool;
    size_t count;
    size_t cap;
    uint32_t free_head;         /* pool index plus one of a reusable slot */
    open_list lists[16 * 128];  /* one per (channel, pitch) */
} open_notes;

static midi_status open_push(open_notes *on, uint8_t channel, uint8_t pitch, uint64_t start, uint8_t velocity) {
    open_list *list = &on->lists[(size_t)channel * 128u + pitch];
    uint32_t slot;
    if (on->free_head) {
        slot = on->free_head;
        on->free_head = on->pool[slot - 1].next;
    } else {
        midi_status status = grow((void **)&on->pool, &on->cap, on->count, sizeof(*on->pool));
        if (status != MIDI_OK) {
            return status;
        }
        if (on->count >= UINT32_MAX - 1u) {
            return MIDI_E_LIMIT;
        }
        slot = (uint32_t)(on->count + 1u);
        on->count++;
    }
    on->pool[slot - 1].start = start;
    on->pool[slot - 1].velocity = velocity;
    on->pool[slot - 1].next = 0;
    if (list->tail) {
        on->pool[list->tail - 1].next = slot;
    } else {
        list->head = slot;
    }
    list->tail = slot;
    return MIDI_OK;
}

/* Removes and returns the oldest open note on (channel, pitch); false when none. */
static bool open_pop(open_notes *on, uint8_t channel, uint8_t pitch, uint64_t *start, uint8_t *velocity) {
    open_list *list = &on->lists[(size_t)channel * 128u + pitch];
    uint32_t slot = list->head;
    if (!slot) {
        return false;
    }
    *start = on->pool[slot - 1].start;
    *velocity = on->pool[slot - 1].velocity;
    list->head = on->pool[slot - 1].next;
    if (!list->head) {
        list->tail = 0;
    }
    on->pool[slot - 1].next = on->free_head;
    on->free_head = slot;
    return true;
}

/* --- one MTrk chunk ------------------------------------------------------- */

typedef struct parse_state {
    const midi_limits *limits;
    uint32_t events_left; /* shared by every track in the file */
    const char *detail;
} parse_state;

static midi_status fail(parse_state *st, midi_status status, const char *detail) {
    st->detail = detail;
    return status;
}

static void keep_text(char *dst, size_t *dst_len, const uint8_t *src, size_t len) {
    if (*dst_len) {
        return; /* the first name wins, as in the Python reader */
    }
    if (len > MIDI_NAME_MAX) {
        len = MIDI_NAME_MAX;
    }
    memcpy(dst, src, len);
    dst[len] = '\0';
    *dst_len = len;
}

static midi_status close_note(midi_track *track, open_notes *on, uint8_t channel, uint8_t pitch, uint64_t tick) {
    uint64_t start;
    uint8_t velocity;
    if (open_pop(on, channel, pitch, &start, &velocity) && tick > start) { /* zero-length notes are artefacts */
        midi_note note;
        note.start = start;
        note.end = tick;
        note.pitch = pitch;
        note.channel = channel;
        note.velocity = velocity;
        PUSH(track->notes, track->note_count, track->note_cap, note);
    }
    return MIDI_OK;
}

static midi_status parse_meta(parse_state *st, cursor *c, midi_track *track, uint64_t tick, bool *end_of_track) {
    uint8_t meta_type;
    uint32_t length;
    const uint8_t *payload;
    midi_status status;

    if ((status = read_byte(c, &meta_type)) != MIDI_OK) {
        return fail(st, status, "the data ends inside a meta event");
    }
    if ((status = read_vlq(c, &length)) != MIDI_OK) {
        return fail(st, status, status == MIDI_E_BAD_VLQ ? "a meta event length is not a valid variable-length quantity"
                                                        : "the data ends inside a meta event length");
    }
    payload = c->data + c->pos;
    if ((status = skip(c, length)) != MIDI_OK) {
        return fail(st, status, "the data ends inside a meta event payload");
    }

    if (meta_type == 0x51 && length == 3) {
        uint32_t usec = ((uint32_t)payload[0] << 16) | ((uint32_t)payload[1] << 8) | (uint32_t)payload[2];
        if (usec > 0) {
            midi_tempo tempo;
            tempo.tick = tick;
            tempo.usec_per_quarter = usec;
            PUSH(track->tempos, track->tempo_count, track->tempo_cap, tempo);
        }
    } else if (meta_type == 0x58 && length >= 2) {
        if (payload[0] >= 1 && payload[1] <= 6) { /* 2^6 = 64th notes */
            midi_time_signature sig;
            sig.tick = tick;
            sig.numerator = payload[0];
            sig.denominator_power = payload[1];
            PUSH(track->time_signatures, track->time_signature_count, track->time_signature_cap, sig);
        }
    } else if (meta_type == 0x59 && length >= 2) {
        int8_t fifths = (int8_t)payload[0];
        if (fifths >= -7 && fifths <= 7) {
            midi_key_signature key;
            key.tick = tick;
            key.fifths = fifths;
            key.minor = payload[1] == 1;
            PUSH(track->key_signatures, track->key_signature_count, track->key_signature_cap, key);
        }
    } else if (meta_type == 0x03) {
        keep_text(track->name, &track->name_len, payload, length);
    } else if (meta_type == 0x04) {
        keep_text(track->instrument, &track->instrument_len, payload, length);
    } else if (meta_type == 0x2F) {
        *end_of_track = true;
    }
    return MIDI_OK;
}

static midi_status parse_track(parse_state *st, const uint8_t *data, size_t len, midi_track *track) {
    cursor c = {data, len, 0};
    open_notes *on;
    uint64_t tick = 0;
    /*
     * Running status: a channel event may omit its status byte and reuse the
     * previous channel status. The spec says meta and sysex events cancel it,
     * but enough sequencers write files that assume otherwise that being
     * strict rejects real music, so only channel messages update `running`.
     */
    uint8_t running = 0;
    midi_status status = MIDI_OK;
    size_t key;

    memset(track->programs, 0xFF, sizeof(track->programs)); /* -1 on every channel */
    on = calloc(1, sizeof(*on));
    if (on == NULL) {
        return fail(st, MIDI_E_NOMEM, "out of memory");
    }

    while (c.pos < c.len) {
        uint32_t delta;
        uint8_t b;
        uint8_t status_byte;
        uint8_t event;
        uint8_t channel;
        bool end_of_track = false;

        if (st->events_left == 0) {
            status = fail(st, MIDI_E_LIMIT, "more events than the limit allows");
            goto done;
        }
        st->events_left--;
        track->events++;

        if ((status = read_vlq(&c, &delta)) != MIDI_OK) {
            fail(st, status, status == MIDI_E_BAD_VLQ ? "a delta time is not a valid variable-length quantity"
                                                     : "the data ends inside a delta time");
            goto done;
        }
        tick += delta;

        if ((status = read_byte(&c, &b)) != MIDI_OK) {
            fail(st, status, "the data ends before an event's status byte");
            goto done;
        }
        if (b & 0x80u) {
            status_byte = b;
            if (b < 0xF0u) {
                running = b;
            }
        } else {
            c.pos--; /* running status: that byte was data, not status */
            if (!running) {
                status = fail(st, MIDI_E_BAD_STATUS, "a data byte appears before any status byte");
                goto done;
            }
            status_byte = running;
        }
        event = status_byte & 0xF0u;
        channel = status_byte & 0x0Fu;

        if (status_byte == 0xFFu) {
            if ((status = parse_meta(st, &c, track, tick, &end_of_track)) != MIDI_OK) {
                goto done;
            }
            if (end_of_track) {
                break;
            }
        } else if (status_byte == 0xF0u || status_byte == 0xF7u) { /* sysex, skipped */
            uint32_t length;
            if ((status = read_vlq(&c, &length)) != MIDI_OK || (status = skip(&c, length)) != MIDI_OK) {
                fail(st, status, "the data ends inside a system exclusive event");
                goto done;
            }
        } else if (event == 0x90u) { /* note on */
            uint8_t pitch, velocity;
            if ((status = read_byte(&c, &pitch)) != MIDI_OK || (status = read_byte(&c, &velocity)) != MIDI_OK) {
                fail(st, status, "the data ends inside a note-on event");
                goto done;
            }
            pitch &= 0x7Fu;
            velocity &= 0x7Fu;
            if (velocity > 0) {
                if ((status = open_push(on, channel, pitch, tick, velocity)) != MIDI_OK) {
                    fail(st, status, "out of memory");
                    goto done;
                }
            } else if ((status = close_note(track, on, channel, pitch, tick)) != MIDI_OK) {
                fail(st, status, "out of memory");
                goto done;
            }
        } else if (event == 0x80u) { /* note off; the release velocity is unused */
            uint8_t pitch, release;
            if ((status = read_byte(&c, &pitch)) != MIDI_OK || (status = read_byte(&c, &release)) != MIDI_OK) {
                fail(st, status, "the data ends inside a note-off event");
                goto done;
            }
            if ((status = close_note(track, on, channel, pitch & 0x7Fu, tick)) != MIDI_OK) {
                fail(st, status, "out of memory");
                goto done;
            }
        } else if (event == 0xB0u) { /* controller */
            uint8_t controller, value;
            if ((status = read_byte(&c, &controller)) != MIDI_OK || (status = read_byte(&c, &value)) != MIDI_OK) {
                fail(st, status, "the data ends inside a controller event");
                goto done;
            }
            if ((controller & 0x7Fu) == SUSTAIN_CONTROLLER) {
                midi_pedal pedal;
                pedal.tick = tick;
                pedal.channel = channel;
                pedal.down = (value & 0x7Fu) >= 64u;
                PUSH(track->pedals, track->pedal_count, track->pedal_cap, pedal);
            }
        } else if (event == 0xC0u) { /* program change; the first one per channel is kept */
            uint8_t program;
            if ((status = read_byte(&c, &program)) != MIDI_OK) {
                fail(st, status, "the data ends inside a program change");
                goto done;
            }
            if (track->programs[channel] < 0) {
                track->programs[channel] = (int16_t)(program & 0x7Fu);
            }
        } else if (event == 0xA0u || event == 0xE0u) { /* aftertouch, pitch bend: two data bytes */
            if ((status = skip(&c, 2)) != MIDI_OK) {
                fail(st, status, "the data ends inside a two-byte channel event");
                goto done;
            }
        } else if (event == 0xD0u) { /* channel pressure: one data byte */
            if ((status = skip(&c, 1)) != MIDI_OK) {
                fail(st, status, "the data ends inside a channel pressure event");
                goto done;
            }
        } else {
            status = fail(st, MIDI_E_BAD_STATUS, "an unrecognised status byte");
            goto done;
        }

        if (track->note_count > st->limits->max_notes) {
            status = fail(st, MIDI_E_LIMIT, "more notes than the limit allows");
            goto done;
        }
    }

    /*
     * Notes still held at the end of the track are closed at the final tick
     * rather than dropped: a truncated file should not silently lose music.
     */
    for (key = 0; key < 16u * 128u; key++) {
        uint64_t start;
        uint8_t velocity;
        uint8_t channel = (uint8_t)(key / 128u);
        uint8_t pitch = (uint8_t)(key % 128u);
        while (open_pop(on, channel, pitch, &start, &velocity)) {
            if (tick > start) {
                midi_note note;
                note.start = start;
                note.end = tick;
                note.pitch = pitch;
                note.channel = channel;
                note.velocity = velocity;
                if ((status = grow((void **)&track->notes, &track->note_cap, track->note_count, sizeof(*track->notes))) != MIDI_OK) {
                    fail(st, status, "out of memory");
                    goto done;
                }
                track->notes[track->note_count++] = note;
                track->unclosed++;
            }
        }
    }
    track->end_tick = tick;
    status = MIDI_OK;

done:
    free(on->pool);
    free(on);
    return status;
}

/* --- the file ------------------------------------------------------------- */

static void free_track(midi_track *track) {
    free(track->notes);
    free(track->tempos);
    free(track->time_signatures);
    free(track->key_signatures);
    free(track->pedals);
    memset(track, 0, sizeof(*track));
}

void midi_free(midi_file *file) {
    size_t i;
    if (file == NULL) {
        return;
    }
    for (i = 0; i < file->track_count; i++) {
        free_track(&file->tracks[i]);
    }
    free(file->tracks);
    memset(file, 0, sizeof(*file));
}

midi_status midi_parse(const uint8_t *data, size_t len, const midi_limits *limits, midi_file *out) {
    parse_state st;
    uint32_t header_len;
    size_t pos;
    size_t track_cap = 0;
    uint32_t chunks = 0;
    midi_status status;

    if (out == NULL) {
        return MIDI_E_NOMEM;
    }
    memset(out, 0, sizeof(*out));
    if (limits == NULL) {
        limits = &MIDI_DEFAULT_LIMITS;
    }
    st.limits = limits;
    st.events_left = limits->max_events;
    st.detail = NULL;

    if (len > limits->max_bytes) {
        out->detail = "the file is larger than the limit allows";
        return MIDI_E_LIMIT;
    }
    if (data == NULL || len < 4 || memcmp(data, "MThd", 4) != 0) {
        out->detail = "no MThd header";
        return MIDI_E_NOT_MIDI;
    }
    if (len < 14) {
        out->detail = "the data ends inside the header";
        return MIDI_E_TRUNCATED;
    }
    header_len = be32(data + 4);
    out->format = be16(data + 8);
    out->declared_tracks = be16(data + 10);
    out->division = be16(data + 12);
    if (header_len < 6) {
        out->detail = "the header is shorter than six bytes";
        return MIDI_E_BAD_HEADER;
    }
    if (out->format > 2) {
        out->detail = "only formats 0, 1 and 2 are supported";
        return MIDI_E_UNSUPPORTED_FORMAT;
    }
    if (out->division & 0x8000u) {
        out->detail = "SMPTE time code is not supported";
        return MIDI_E_SMPTE;
    }
    if (out->division == 0) {
        out->detail = "the division is zero";
        return MIDI_E_BAD_HEADER;
    }
    if (out->declared_tracks > limits->max_tracks) {
        out->detail = "more tracks than the limit allows";
        return MIDI_E_LIMIT;
    }
    if (header_len > len - 8) {
        /* The header claims more than the file holds; nothing follows it. */
        pos = len;
    } else {
        pos = 8 + (size_t)header_len;
    }

    while (pos + 8 <= len && out->track_count < out->declared_tracks) {
        size_t available;
        size_t chunk_len;
        const uint8_t *body;
        chunks++;
        if (chunks > limits->max_tracks * 4u) {
            midi_free(out);
            out->detail = "more chunks than the limit allows";
            return MIDI_E_LIMIT;
        }
        chunk_len = be32(data + pos + 4);
        available = len - (pos + 8);
        if (chunk_len > available) {
            out->truncated = true;
            chunk_len = available;
        }
        body = data + pos + 8;
        if (memcmp(data + pos, "MTrk", 4) == 0) { /* other chunk types are legal and ignored */
            midi_track *track;
            if ((status = grow((void **)&out->tracks, &track_cap, out->track_count, sizeof(*out->tracks))) != MIDI_OK) {
                midi_free(out);
                out->detail = "out of memory";
                return status;
            }
            track = &out->tracks[out->track_count];
            memset(track, 0, sizeof(*track));
            out->track_count++;
            if ((status = parse_track(&st, body, chunk_len, track)) != MIDI_OK) {
                midi_free(out);
                out->detail = st.detail;
                return status;
            }
        }
        pos += 8 + chunk_len;
    }

    if (out->track_count == 0) {
        midi_free(out);
        out->detail = "no MTrk chunk";
        return MIDI_E_NO_TRACKS;
    }
    return MIDI_OK;
}

const char *midi_status_name(midi_status status) {
    switch (status) {
    case MIDI_OK:
        return "MIDI_OK";
    case MIDI_E_NOT_MIDI:
        return "MIDI_E_NOT_MIDI";
    case MIDI_E_BAD_HEADER:
        return "MIDI_E_BAD_HEADER";
    case MIDI_E_UNSUPPORTED_FORMAT:
        return "MIDI_E_UNSUPPORTED_FORMAT";
    case MIDI_E_SMPTE:
        return "MIDI_E_SMPTE";
    case MIDI_E_TRUNCATED:
        return "MIDI_E_TRUNCATED";
    case MIDI_E_BAD_VLQ:
        return "MIDI_E_BAD_VLQ";
    case MIDI_E_BAD_STATUS:
        return "MIDI_E_BAD_STATUS";
    case MIDI_E_NO_TRACKS:
        return "MIDI_E_NO_TRACKS";
    case MIDI_E_LIMIT:
        return "MIDI_E_LIMIT";
    case MIDI_E_NOMEM:
        return "MIDI_E_NOMEM";
    default:
        return "MIDI_E_UNKNOWN";
    }
}
