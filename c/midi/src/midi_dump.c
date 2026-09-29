/*
 * midi_dump: parse a Standard MIDI File and print what was read as JSON.
 *
 *   midi_dump FILE [--max-bytes N] [--max-tracks N] [--max-notes N] [--max-events N]
 *
 * On success the JSON document is printed and the exit status is 0. On a
 * parse error a document {"error": NAME, "detail": SENTENCE} is printed and
 * the exit status is 1; a usage or I/O problem exits with 2. The document is
 * what scripts/midi_diff.py compares with the Python reader's view.
 */
#include "midi.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void print_string(const char *bytes, size_t len) {
    size_t i;
    putchar('"');
    for (i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)bytes[i];
        if (ch == '"' || ch == '\\') {
            printf("\\%c", ch);
        } else if (ch < 0x20 || ch >= 0x7F) {
            /* Names are raw bytes; each is shown as its own code point. */
            printf("\\u%04x", (unsigned)ch);
        } else {
            putchar((int)ch);
        }
    }
    putchar('"');
}

static void print_track(const midi_track *t) {
    size_t i;
    int first;
    printf("{\"name\":");
    print_string(t->name, t->name_len);
    printf(",\"instrument\":");
    print_string(t->instrument, t->instrument_len);
    printf(",\"end_tick\":%" PRIu64 ",\"events\":%" PRIu32 ",\"unclosed\":%" PRIu32, t->end_tick, t->events, t->unclosed);
    printf(",\"programs\":[");
    for (i = 0, first = 1; i < 16; i++) {
        if (t->programs[i] >= 0) {
            printf("%s[%zu,%d]", first ? "" : ",", i, (int)t->programs[i]);
            first = 0;
        }
    }
    printf("],\"notes\":[");
    for (i = 0; i < t->note_count; i++) {
        const midi_note *n = &t->notes[i];
        printf("%s[%" PRIu64 ",%" PRIu64 ",%u,%u,%u]", i ? "," : "", n->start, n->end, n->pitch, n->channel, n->velocity);
    }
    printf("],\"tempos\":[");
    for (i = 0; i < t->tempo_count; i++) {
        printf("%s[%" PRIu64 ",%" PRIu32 "]", i ? "," : "", t->tempos[i].tick, t->tempos[i].usec_per_quarter);
    }
    printf("],\"time_signatures\":[");
    for (i = 0; i < t->time_signature_count; i++) {
        const midi_time_signature *s = &t->time_signatures[i];
        printf("%s[%" PRIu64 ",%u,%u]", i ? "," : "", s->tick, s->numerator, 1u << s->denominator_power);
    }
    printf("],\"key_signatures\":[");
    for (i = 0; i < t->key_signature_count; i++) {
        const midi_key_signature *k = &t->key_signatures[i];
        printf("%s[%" PRIu64 ",%d,\"%s\"]", i ? "," : "", k->tick, (int)k->fifths, k->minor ? "minor" : "major");
    }
    printf("],\"pedal\":[");
    for (i = 0; i < t->pedal_count; i++) {
        const midi_pedal *p = &t->pedals[i];
        printf("%s[%" PRIu64 ",%u,%s]", i ? "," : "", p->tick, p->channel, p->down ? "true" : "false");
    }
    printf("]}");
}

static int usage(const char *argv0) {
    fprintf(stderr, "usage: %s FILE [--max-bytes N] [--max-tracks N] [--max-notes N] [--max-events N]\n", argv0);
    return 2;
}

int main(int argc, char **argv) {
    midi_limits limits = MIDI_DEFAULT_LIMITS;
    const char *path = NULL;
    FILE *in;
    uint8_t *data = NULL;
    size_t len = 0, cap = 0;
    midi_file file;
    midi_status status;
    size_t i;
    int arg;

    for (arg = 1; arg < argc; arg++) {
        const char *a = argv[arg];
        if (strcmp(a, "--max-bytes") == 0 || strcmp(a, "--max-tracks") == 0 || strcmp(a, "--max-notes") == 0 ||
            strcmp(a, "--max-events") == 0) {
            char *end;
            unsigned long long value;
            if (arg + 1 >= argc) {
                return usage(argv[0]);
            }
            errno = 0;
            value = strtoull(argv[arg + 1], &end, 10);
            if (errno != 0 || *end != '\0' || value > UINT32_MAX) {
                return usage(argv[0]);
            }
            if (strcmp(a, "--max-bytes") == 0) {
                limits.max_bytes = (size_t)value;
            } else if (strcmp(a, "--max-tracks") == 0) {
                limits.max_tracks = (uint32_t)value;
            } else if (strcmp(a, "--max-notes") == 0) {
                limits.max_notes = (uint32_t)value;
            } else {
                limits.max_events = (uint32_t)value;
            }
            arg++;
        } else if (path == NULL) {
            path = a;
        } else {
            return usage(argv[0]);
        }
    }
    if (path == NULL) {
        return usage(argv[0]);
    }

    in = fopen(path, "rb");
    if (in == NULL) {
        fprintf(stderr, "%s: cannot open: %s\n", path, strerror(errno));
        return 2;
    }
    /* Read at most one byte past the limit: enough to know it was exceeded. */
    for (;;) {
        size_t got;
        if (len == cap) {
            size_t next = cap ? cap * 2 : 65536;
            uint8_t *grown;
            if (next > limits.max_bytes + 1) {
                next = limits.max_bytes + 1;
            }
            if (next <= cap) {
                break;
            }
            grown = realloc(data, next);
            if (grown == NULL) {
                fprintf(stderr, "out of memory\n");
                free(data);
                fclose(in);
                return 2;
            }
            data = grown;
            cap = next;
        }
        got = fread(data + len, 1, cap - len, in);
        if (got == 0) {
            break;
        }
        len += got;
    }
    if (ferror(in)) {
        fprintf(stderr, "%s: read error\n", path);
        free(data);
        fclose(in);
        return 2;
    }
    fclose(in);

    status = midi_parse(data, len, &limits, &file);
    if (status != MIDI_OK) {
        printf("{\"error\":\"%s\",\"detail\":", midi_status_name(status));
        print_string(file.detail ? file.detail : "", file.detail ? strlen(file.detail) : 0);
        printf("}\n");
        midi_free(&file);
        free(data);
        return 1;
    }

    printf("{\"format\":%u,\"division\":%u,\"declared_tracks\":%u,\"truncated\":%s,\"tracks\":[", file.format, file.division,
           file.declared_tracks, file.truncated ? "true" : "false");
    for (i = 0; i < file.track_count; i++) {
        if (i) {
            putchar(',');
        }
        print_track(&file.tracks[i]);
    }
    printf("]}\n");
    midi_free(&file);
    free(data);
    return 0;
}
