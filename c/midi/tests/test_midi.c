/*
 * Unit tests for the MIDI parser, with Unity. Files are built byte by byte
 * here rather than checked in, so each test shows exactly which bytes it is
 * about. Every malformed case has a well-formed neighbour that must still
 * parse, so a fix for one cannot quietly refuse the other.
 */
#include "midi.h"
#include "unity.h"

#include <stdlib.h>
#include <string.h>

/* --- byte builders -------------------------------------------------------- */

typedef struct buffer {
    uint8_t bytes[4096];
    size_t len;
} buffer;

static void put(buffer *b, const uint8_t *bytes, size_t n) {
    TEST_ASSERT_TRUE_MESSAGE(b->len + n <= sizeof(b->bytes), "test buffer overflow");
    memcpy(b->bytes + b->len, bytes, n);
    b->len += n;
}

static void put1(buffer *b, uint8_t v) {
    put(b, &v, 1);
}

static void put_vlq(buffer *b, uint32_t value) {
    uint8_t out[5];
    int n = 0;
    out[n++] = (uint8_t)(value & 0x7Fu);
    value >>= 7;
    while (value) {
        out[n++] = (uint8_t)((value & 0x7Fu) | 0x80u);
        value >>= 7;
    }
    while (n) {
        put1(b, out[--n]);
    }
}

static void header(buffer *b, uint16_t format, uint16_t tracks, uint16_t division) {
    const uint8_t fixed[] = {'M', 'T', 'h', 'd', 0, 0, 0, 6};
    put(b, fixed, sizeof(fixed));
    put1(b, (uint8_t)(format >> 8));
    put1(b, (uint8_t)format);
    put1(b, (uint8_t)(tracks >> 8));
    put1(b, (uint8_t)tracks);
    put1(b, (uint8_t)(division >> 8));
    put1(b, (uint8_t)division);
}

/* An MTrk chunk around `body`, with an end-of-track event appended when asked. */
static void track(buffer *b, const buffer *body, int end_of_track) {
    const uint8_t eot[] = {0x00, 0xFF, 0x2F, 0x00};
    size_t len = body->len + (end_of_track ? sizeof(eot) : 0);
    const uint8_t id[] = {'M', 'T', 'r', 'k'};
    put(b, id, 4);
    put1(b, (uint8_t)(len >> 24));
    put1(b, (uint8_t)(len >> 16));
    put1(b, (uint8_t)(len >> 8));
    put1(b, (uint8_t)len);
    put(b, body->bytes, body->len);
    if (end_of_track) {
        put(b, eot, sizeof(eot));
    }
}

static void note_on(buffer *b, uint32_t delta, uint8_t channel, uint8_t pitch, uint8_t velocity) {
    put_vlq(b, delta);
    put1(b, (uint8_t)(0x90u | channel));
    put1(b, pitch);
    put1(b, velocity);
}

static void note_off(buffer *b, uint32_t delta, uint8_t channel, uint8_t pitch) {
    put_vlq(b, delta);
    put1(b, (uint8_t)(0x80u | channel));
    put1(b, pitch);
    put1(b, 0x40);
}

static void meta(buffer *b, uint32_t delta, uint8_t type, const uint8_t *payload, size_t n) {
    put_vlq(b, delta);
    put1(b, 0xFF);
    put1(b, type);
    put_vlq(b, (uint32_t)n);
    put(b, payload, n);
}

/* A format 0 file with one C major triad played as a melody: C4 D4 E4. */
static void simple_file(buffer *file) {
    buffer body = {{0}, 0};
    const uint8_t tempo[] = {0x07, 0xA1, 0x20}; /* 500000 usec = 120 bpm */
    const uint8_t sig[] = {3, 2, 24, 8};        /* 3/4 */
    const uint8_t name[] = {'T', 'u', 'n', 'e'};
    meta(&body, 0, 0x03, name, sizeof(name));
    meta(&body, 0, 0x51, tempo, sizeof(tempo));
    meta(&body, 0, 0x58, sig, sizeof(sig));
    note_on(&body, 0, 0, 60, 100);
    note_off(&body, 480, 0, 60);
    note_on(&body, 0, 0, 62, 90);
    note_off(&body, 480, 0, 62);
    note_on(&body, 0, 0, 64, 80);
    note_off(&body, 480, 0, 64);
    header(file, 0, 1, 480);
    track(file, &body, 1);
}

static midi_file parsed;

void setUp(void) {
    memset(&parsed, 0, sizeof(parsed));
}

void tearDown(void) {
    midi_free(&parsed);
}

/* --- variable-length quantities ------------------------------------------- */

static void test_vlq_single_byte(void) {
    const uint8_t data[] = {0x00, 0x7F};
    size_t pos = 0;
    uint32_t value = 99;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_read_vlq(data, sizeof(data), &pos, &value));
    TEST_ASSERT_EQUAL_UINT32(0, value);
    TEST_ASSERT_EQUAL_size_t(1, pos);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_read_vlq(data, sizeof(data), &pos, &value));
    TEST_ASSERT_EQUAL_UINT32(127, value);
    TEST_ASSERT_EQUAL_size_t(2, pos);
}

static void test_vlq_multi_byte(void) {
    const uint8_t data[] = {0x81, 0x00, 0xFF, 0xFF, 0xFF, 0x7F};
    size_t pos = 0;
    uint32_t value = 0;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_read_vlq(data, sizeof(data), &pos, &value));
    TEST_ASSERT_EQUAL_UINT32(128, value);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_read_vlq(data, sizeof(data), &pos, &value));
    TEST_ASSERT_EQUAL_UINT32(0x0FFFFFFFu, value);
    TEST_ASSERT_EQUAL_size_t(6, pos);
}

static void test_vlq_refuses_five_bytes_and_a_short_buffer(void) {
    const uint8_t five[] = {0x81, 0x81, 0x81, 0x81, 0x00};
    const uint8_t cut[] = {0x81, 0x81};
    size_t pos = 0;
    uint32_t value = 0;
    TEST_ASSERT_EQUAL(MIDI_E_BAD_VLQ, midi_read_vlq(five, sizeof(five), &pos, &value));
    pos = 0;
    TEST_ASSERT_EQUAL(MIDI_E_TRUNCATED, midi_read_vlq(cut, sizeof(cut), &pos, &value));
    pos = 0;
    TEST_ASSERT_EQUAL(MIDI_E_TRUNCATED, midi_read_vlq(cut, 0, &pos, &value));
}

/* --- headers -------------------------------------------------------------- */

static void test_a_simple_file_parses(void) {
    buffer file = {{0}, 0};
    const midi_track *t;
    simple_file(&file);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_UINT16(0, parsed.format);
    TEST_ASSERT_EQUAL_UINT16(1, parsed.declared_tracks);
    TEST_ASSERT_EQUAL_UINT16(480, parsed.division);
    TEST_ASSERT_EQUAL_size_t(1, parsed.track_count);
    TEST_ASSERT_FALSE(parsed.truncated);
    t = &parsed.tracks[0];
    TEST_ASSERT_EQUAL_STRING("Tune", t->name);
    TEST_ASSERT_EQUAL_size_t(3, t->note_count);
    TEST_ASSERT_EQUAL_UINT64(0, t->notes[0].start);
    TEST_ASSERT_EQUAL_UINT64(480, t->notes[0].end);
    TEST_ASSERT_EQUAL_UINT8(60, t->notes[0].pitch);
    TEST_ASSERT_EQUAL_UINT8(100, t->notes[0].velocity);
    TEST_ASSERT_EQUAL_UINT64(960, t->notes[2].start);
    TEST_ASSERT_EQUAL_UINT64(1440, t->notes[2].end);
    TEST_ASSERT_EQUAL_UINT8(64, t->notes[2].pitch);
    TEST_ASSERT_EQUAL_size_t(1, t->tempo_count);
    TEST_ASSERT_EQUAL_UINT32(500000, t->tempos[0].usec_per_quarter);
    TEST_ASSERT_EQUAL_size_t(1, t->time_signature_count);
    TEST_ASSERT_EQUAL_UINT8(3, t->time_signatures[0].numerator);
    TEST_ASSERT_EQUAL_UINT8(2, t->time_signatures[0].denominator_power);
    TEST_ASSERT_EQUAL_UINT64(1440, t->end_tick);
    TEST_ASSERT_EQUAL_UINT32(10, t->events); /* three metas, six notes, end of track */
    TEST_ASSERT_EQUAL_UINT32(0, t->unclosed);
    TEST_ASSERT_EQUAL_INT16(-1, t->programs[0]);
}

static void test_not_a_midi_file(void) {
    const uint8_t junk[] = "RIFF....WAVEfmt ";
    TEST_ASSERT_EQUAL(MIDI_E_NOT_MIDI, midi_parse(junk, sizeof(junk), NULL, &parsed));
    TEST_ASSERT_NOT_NULL(parsed.detail);
    TEST_ASSERT_EQUAL(MIDI_E_NOT_MIDI, midi_parse(junk, 0, NULL, &parsed));
    TEST_ASSERT_EQUAL(MIDI_E_NOT_MIDI, midi_parse(NULL, 0, NULL, &parsed));
}

static void test_header_too_short(void) {
    buffer file = {{0}, 0};
    header(&file, 0, 1, 480);
    TEST_ASSERT_EQUAL(MIDI_E_TRUNCATED, midi_parse(file.bytes, 10, NULL, &parsed));
    TEST_ASSERT_EQUAL(MIDI_E_TRUNCATED, midi_parse(file.bytes, 13, NULL, &parsed));
}

static void test_header_length_and_division_are_checked(void) {
    buffer file = {{0}, 0};
    simple_file(&file);
    file.bytes[7] = 5; /* header length 5 */
    TEST_ASSERT_EQUAL(MIDI_E_BAD_HEADER, midi_parse(file.bytes, file.len, NULL, &parsed));
    file.bytes[7] = 6;
    file.bytes[12] = 0;
    file.bytes[13] = 0; /* division zero */
    TEST_ASSERT_EQUAL(MIDI_E_BAD_HEADER, midi_parse(file.bytes, file.len, NULL, &parsed));
    file.bytes[12] = 0xE7;
    file.bytes[13] = 0x28; /* SMPTE: 25 frames, 40 ticks */
    TEST_ASSERT_EQUAL(MIDI_E_SMPTE, midi_parse(file.bytes, file.len, NULL, &parsed));
    file.bytes[12] = 0x01;
    file.bytes[13] = 0xE0;
    file.bytes[9] = 3; /* format 3 */
    TEST_ASSERT_EQUAL(MIDI_E_UNSUPPORTED_FORMAT, midi_parse(file.bytes, file.len, NULL, &parsed));
    file.bytes[9] = 0;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed)); /* the near miss still parses */
}

static void test_a_longer_header_is_skipped(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t fixed[] = {'M', 'T', 'h', 'd', 0, 0, 0, 8, 0, 1, 0, 1, 0x01, 0xE0, 0xAA, 0xBB};
    put(&file, fixed, sizeof(fixed));
    note_on(&body, 0, 0, 60, 64);
    note_off(&body, 10, 0, 60);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].note_count);
}

/* --- formats and chunks --------------------------------------------------- */

static void test_format_1_and_2_with_several_tracks(void) {
    buffer file = {{0}, 0};
    buffer a = {{0}, 0};
    buffer b = {{0}, 0};
    const uint8_t tempo[] = {0x0F, 0x42, 0x40}; /* 1000000 usec = 60 bpm */
    meta(&a, 0, 0x51, tempo, sizeof(tempo));
    note_on(&b, 0, 1, 48, 70);
    note_off(&b, 240, 1, 48);
    header(&file, 1, 2, 96);
    track(&file, &a, 1);
    track(&file, &b, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_UINT16(1, parsed.format);
    TEST_ASSERT_EQUAL_size_t(2, parsed.track_count);
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].tempo_count);
    TEST_ASSERT_EQUAL_size_t(0, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[1].note_count);
    TEST_ASSERT_EQUAL_UINT8(1, parsed.tracks[1].notes[0].channel);
    midi_free(&parsed);
    file.bytes[9] = 2; /* format 2: independent tracks, parsed the same way */
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_UINT16(2, parsed.format);
    TEST_ASSERT_EQUAL_size_t(2, parsed.track_count);
}

static void test_unknown_chunks_are_skipped_and_missing_tracks_reported(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t other[] = {'X', 'F', 'I', 'H', 0, 0, 0, 2, 0xAB, 0xCD};
    header(&file, 1, 3, 480); /* three promised, one present */
    put(&file, other, sizeof(other));
    note_on(&body, 0, 0, 60, 64);
    note_off(&body, 5, 0, 60);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_UINT16(3, parsed.declared_tracks);
    TEST_ASSERT_EQUAL_size_t(1, parsed.track_count);
}

static void test_no_tracks_is_an_error(void) {
    buffer file = {{0}, 0};
    const uint8_t other[] = {'X', 'F', 'I', 'H', 0, 0, 0, 0};
    header(&file, 0, 1, 480);
    TEST_ASSERT_EQUAL(MIDI_E_NO_TRACKS, midi_parse(file.bytes, file.len, NULL, &parsed));
    put(&file, other, sizeof(other));
    TEST_ASSERT_EQUAL(MIDI_E_NO_TRACKS, midi_parse(file.bytes, file.len, NULL, &parsed));
}

/* --- notes ---------------------------------------------------------------- */

static void test_velocity_zero_closes_a_note_and_zero_length_notes_are_dropped(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    note_on(&body, 0, 0, 60, 64);
    note_on(&body, 100, 0, 60, 0); /* note-on with velocity 0 is a note-off */
    note_on(&body, 0, 0, 62, 64);
    note_off(&body, 0, 0, 62);     /* zero length: dropped */
    header(&file, 0, 1, 480);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(100, parsed.tracks[0].notes[0].end);
}

static void test_retriggered_pitch_closes_oldest_first(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    note_on(&body, 0, 0, 60, 64);
    note_on(&body, 10, 0, 60, 65);
    note_off(&body, 10, 0, 60); /* closes the note started at 0 */
    note_off(&body, 10, 0, 60); /* closes the note started at 10 */
    header(&file, 0, 1, 480);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(2, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(0, parsed.tracks[0].notes[0].start);
    TEST_ASSERT_EQUAL_UINT64(20, parsed.tracks[0].notes[0].end);
    TEST_ASSERT_EQUAL_UINT8(64, parsed.tracks[0].notes[0].velocity);
    TEST_ASSERT_EQUAL_UINT64(10, parsed.tracks[0].notes[1].start);
    TEST_ASSERT_EQUAL_UINT64(30, parsed.tracks[0].notes[1].end);
    TEST_ASSERT_EQUAL_UINT8(65, parsed.tracks[0].notes[1].velocity);
}

static void test_a_stray_note_off_is_ignored(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    note_off(&body, 0, 0, 60);
    note_on(&body, 0, 3, 60, 64);
    note_off(&body, 7, 5, 60); /* other channel: not this note */
    note_off(&body, 0, 3, 60);
    header(&file, 0, 1, 480);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT8(3, parsed.tracks[0].notes[0].channel);
    TEST_ASSERT_EQUAL_UINT64(7, parsed.tracks[0].notes[0].end);
}

static void test_notes_still_held_are_closed_at_the_end_of_the_track(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    note_on(&body, 0, 0, 60, 64);
    note_on(&body, 50, 0, 67, 64);
    note_on(&body, 50, 0, 60, 64); /* a second C, never closed */
    header(&file, 0, 1, 480);
    track(&file, &body, 1); /* end of track at tick 100 */
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(2, parsed.tracks[0].note_count); /* the C at tick 100 has zero length */
    TEST_ASSERT_EQUAL_UINT32(2, parsed.tracks[0].unclosed);
    TEST_ASSERT_EQUAL_UINT64(100, parsed.tracks[0].notes[0].end);
    TEST_ASSERT_EQUAL_UINT64(100, parsed.tracks[0].notes[1].end);
    TEST_ASSERT_EQUAL_UINT64(100, parsed.tracks[0].end_tick);
}

/* --- running status ------------------------------------------------------- */

static void test_running_status_reuses_the_previous_channel_status(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t chord[] = {
        0x00, 0x90, 60, 64, /* note on, status given */
        0x00, 64, 64,       /* note on, status omitted */
        0x00, 67, 64,       /* note on, status omitted */
        0x60, 60, 0,        /* note off by velocity 0, status omitted */
        0x00, 64, 0,        /* ... */
        0x00, 67, 0,        /* ... */
    };
    put(&body, chord, sizeof(chord));
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(3, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(96, parsed.tracks[0].notes[2].end);
    TEST_ASSERT_EQUAL_UINT8(67, parsed.tracks[0].notes[2].pitch);
}

static void test_running_status_survives_a_meta_event(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t name[] = {'A'};
    const uint8_t rest[] = {0x10, 60, 0};
    note_on(&body, 0, 0, 60, 64);
    meta(&body, 0, 0x03, name, sizeof(name)); /* the spec says this cancels running status; real files disagree */
    put(&body, rest, sizeof(rest));
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(16, parsed.tracks[0].notes[0].end);
}

static void test_a_data_byte_before_any_status_is_refused(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t bare[] = {0x00, 60, 64};
    put(&body, bare, sizeof(bare));
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_E_BAD_STATUS, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(0, parsed.track_count); /* nothing is left behind */
}

static void test_an_unknown_status_byte_is_refused(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t bad[] = {0x00, 0xF1, 0x00};
    note_on(&body, 0, 0, 60, 64);
    put(&body, bad, sizeof(bad));
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_E_BAD_STATUS, midi_parse(file.bytes, file.len, NULL, &parsed));
}

/* --- the other events ----------------------------------------------------- */

static void test_meta_events_are_read_or_ignored_by_their_shape(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t tempo_short[] = {0x07, 0xA1};          /* wrong length: ignored */
    const uint8_t tempo_zero[] = {0x00, 0x00, 0x00};     /* zero: ignored */
    const uint8_t tempo_ok[] = {0x06, 0x1A, 0x80};       /* 400000 usec */
    const uint8_t sig_bad_den[] = {4, 7, 24, 8};         /* 2^7: ignored */
    const uint8_t sig_bad_num[] = {0, 2, 24, 8};         /* numerator 0: ignored */
    const uint8_t sig_ok[] = {6, 3};                     /* 6/8, two bytes is enough */
    const uint8_t key_out[] = {0x08, 0x00};              /* 8 sharps: ignored */
    const uint8_t key_minor[] = {0xFD, 0x01};            /* -3, minor */
    const uint8_t key_major[] = {0x02, 0x00};            /* 2, major */
    const uint8_t name_a[] = {'F', 'i', 'r', 's', 't'};
    const uint8_t name_b[] = {'S', 'e', 'c', 'o', 'n', 'd'};
    const uint8_t instrument[] = {'P', 'i', 'a', 'n', 'o'};
    const uint8_t lyric[] = {'l', 'a'};
    meta(&body, 0, 0x51, tempo_short, sizeof(tempo_short));
    meta(&body, 0, 0x51, tempo_zero, sizeof(tempo_zero));
    meta(&body, 0, 0x51, tempo_ok, sizeof(tempo_ok));
    meta(&body, 0, 0x58, sig_bad_den, sizeof(sig_bad_den));
    meta(&body, 0, 0x58, sig_bad_num, sizeof(sig_bad_num));
    meta(&body, 0, 0x58, sig_ok, sizeof(sig_ok));
    meta(&body, 0, 0x59, key_out, sizeof(key_out));
    meta(&body, 0, 0x59, key_minor, sizeof(key_minor));
    meta(&body, 0, 0x59, key_major, sizeof(key_major));
    meta(&body, 0, 0x03, name_a, sizeof(name_a));
    meta(&body, 0, 0x03, name_b, sizeof(name_b)); /* the first name wins */
    meta(&body, 0, 0x04, instrument, sizeof(instrument));
    meta(&body, 0, 0x05, lyric, sizeof(lyric));   /* lyrics are ignored */
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].tempo_count);
    TEST_ASSERT_EQUAL_UINT32(400000, parsed.tracks[0].tempos[0].usec_per_quarter);
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].time_signature_count);
    TEST_ASSERT_EQUAL_UINT8(6, parsed.tracks[0].time_signatures[0].numerator);
    TEST_ASSERT_EQUAL_UINT8(3, parsed.tracks[0].time_signatures[0].denominator_power);
    TEST_ASSERT_EQUAL_size_t(2, parsed.tracks[0].key_signature_count);
    TEST_ASSERT_EQUAL_INT8(-3, parsed.tracks[0].key_signatures[0].fifths);
    TEST_ASSERT_TRUE(parsed.tracks[0].key_signatures[0].minor);
    TEST_ASSERT_EQUAL_INT8(2, parsed.tracks[0].key_signatures[1].fifths);
    TEST_ASSERT_FALSE(parsed.tracks[0].key_signatures[1].minor);
    TEST_ASSERT_EQUAL_STRING("First", parsed.tracks[0].name);
    TEST_ASSERT_EQUAL_STRING("Piano", parsed.tracks[0].instrument);
}

static void test_long_names_are_cut_not_overflowed(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    uint8_t name[300];
    memset(name, 'x', sizeof(name));
    meta(&body, 0, 0x03, name, sizeof(name));
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(MIDI_NAME_MAX, parsed.tracks[0].name_len);
    TEST_ASSERT_EQUAL_size_t(MIDI_NAME_MAX, strlen(parsed.tracks[0].name));
}

static void test_end_of_track_stops_reading(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t eot[] = {0x00, 0xFF, 0x2F, 0x00};
    note_on(&body, 0, 0, 60, 64);
    note_off(&body, 10, 0, 60);
    put(&body, eot, sizeof(eot));
    note_on(&body, 0, 0, 62, 64); /* after the end: never read */
    note_off(&body, 10, 0, 62);
    header(&file, 0, 1, 96);
    track(&file, &body, 0);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(10, parsed.tracks[0].end_tick);
}

static void test_sysex_controllers_programs_and_the_rest(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t sysex[] = {0x00, 0xF0, 0x03, 0x43, 0x12, 0xF7};
    const uint8_t pedal_down[] = {0x00, 0xB0, 64, 127};
    const uint8_t volume[] = {0x00, 0xB0, 7, 100};      /* other controllers are ignored */
    const uint8_t pedal_up[] = {0x10, 0xB0, 64, 0};
    const uint8_t program_a[] = {0x00, 0xC1, 5};
    const uint8_t program_b[] = {0x00, 0xC1, 9};        /* the first program wins */
    const uint8_t aftertouch[] = {0x00, 0xA0, 60, 50};
    const uint8_t pressure[] = {0x00, 0xD0, 50};
    const uint8_t bend[] = {0x00, 0xE0, 0x00, 0x40};
    put(&body, sysex, sizeof(sysex));
    put(&body, pedal_down, sizeof(pedal_down));
    put(&body, volume, sizeof(volume));
    put(&body, pedal_up, sizeof(pedal_up));
    put(&body, program_a, sizeof(program_a));
    put(&body, program_b, sizeof(program_b));
    put(&body, aftertouch, sizeof(aftertouch));
    put(&body, pressure, sizeof(pressure));
    put(&body, bend, sizeof(bend));
    note_on(&body, 0, 0, 60, 64);
    note_off(&body, 10, 0, 60);
    header(&file, 0, 1, 96);
    track(&file, &body, 1);
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(2, parsed.tracks[0].pedal_count);
    TEST_ASSERT_TRUE(parsed.tracks[0].pedals[0].down);
    TEST_ASSERT_EQUAL_UINT64(0, parsed.tracks[0].pedals[0].tick);
    TEST_ASSERT_FALSE(parsed.tracks[0].pedals[1].down);
    TEST_ASSERT_EQUAL_UINT64(16, parsed.tracks[0].pedals[1].tick);
    TEST_ASSERT_EQUAL_INT16(5, parsed.tracks[0].programs[1]);
    TEST_ASSERT_EQUAL_INT16(-1, parsed.tracks[0].programs[0]);
    TEST_ASSERT_EQUAL_size_t(1, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(16, parsed.tracks[0].notes[0].start);
}

/* --- truncation ----------------------------------------------------------- */

static void test_a_cut_inside_an_event_is_an_error(void) {
    buffer file = {{0}, 0};
    simple_file(&file);
    /* Cut one byte into the last note-off: the chunk length now overruns. */
    TEST_ASSERT_EQUAL(MIDI_E_TRUNCATED, midi_parse(file.bytes, file.len - 6, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(0, parsed.track_count);
}

static void test_a_cut_between_events_parses_what_is_there(void) {
    buffer file = {{0}, 0};
    simple_file(&file);
    /* Drop the end-of-track event (4 bytes): the chunk overruns by four bytes. */
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len - 4, NULL, &parsed));
    TEST_ASSERT_TRUE(parsed.truncated);
    TEST_ASSERT_EQUAL_size_t(3, parsed.tracks[0].note_count);
    TEST_ASSERT_EQUAL_UINT64(1440, parsed.tracks[0].end_tick);
}

static void test_every_prefix_of_a_file_is_handled(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    buffer full = {{0}, 0};
    size_t cut;
    const uint8_t tempo[] = {0x07, 0xA1, 0x20};
    const uint8_t sysex[] = {0x00, 0xF0, 0x02, 0x43, 0xF7};
    meta(&body, 0, 0x51, tempo, sizeof(tempo));
    put(&body, sysex, sizeof(sysex));
    note_on(&body, 0, 0, 60, 64);
    note_on(&body, 0, 0, 64, 64);
    note_off(&body, 0x81, 0, 60); /* a two-byte delta */
    note_off(&body, 0, 0, 64);
    header(&file, 1, 2, 480);
    track(&file, &body, 1);
    track(&file, &body, 1);
    put(&full, file.bytes, file.len);
    /* No prefix may crash, hang or leak; each is either parsed or refused. */
    for (cut = 0; cut <= full.len; cut++) {
        midi_status status = midi_parse(full.bytes, cut, NULL, &parsed);
        if (status == MIDI_OK) {
            TEST_ASSERT_TRUE(parsed.track_count >= 1);
        } else {
            TEST_ASSERT_EQUAL_size_t(0, parsed.track_count);
            TEST_ASSERT_NOT_NULL(parsed.detail);
        }
        midi_free(&parsed);
    }
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(full.bytes, full.len, NULL, &parsed));
    TEST_ASSERT_EQUAL_size_t(2, parsed.track_count);
    TEST_ASSERT_EQUAL_size_t(2, parsed.tracks[1].note_count);
}

static void test_every_single_byte_corruption_is_handled(void) {
    buffer file = {{0}, 0};
    size_t at;
    simple_file(&file);
    for (at = 0; at < file.len; at++) {
        uint8_t saved = file.bytes[at];
        file.bytes[at] ^= 0xFF;
        (void)midi_parse(file.bytes, file.len, NULL, &parsed);
        midi_free(&parsed);
        file.bytes[at] = saved;
    }
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, NULL, &parsed));
}

/* --- limits --------------------------------------------------------------- */

static void test_limits_are_enforced_and_near_misses_pass(void) {
    buffer file = {{0}, 0};
    midi_limits limits = MIDI_DEFAULT_LIMITS;
    simple_file(&file);

    limits.max_bytes = file.len - 1;
    TEST_ASSERT_EQUAL(MIDI_E_LIMIT, midi_parse(file.bytes, file.len, &limits, &parsed));
    limits.max_bytes = file.len;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, &limits, &parsed));
    midi_free(&parsed);
    limits = MIDI_DEFAULT_LIMITS;

    limits.max_tracks = 0; /* the header declares one */
    TEST_ASSERT_EQUAL(MIDI_E_LIMIT, midi_parse(file.bytes, file.len, &limits, &parsed));
    limits.max_tracks = 1;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, &limits, &parsed));
    midi_free(&parsed);
    limits = MIDI_DEFAULT_LIMITS;

    limits.max_events = 9; /* the file has ten */
    TEST_ASSERT_EQUAL(MIDI_E_LIMIT, midi_parse(file.bytes, file.len, &limits, &parsed));
    limits.max_events = 10;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, &limits, &parsed));
    midi_free(&parsed);
    limits = MIDI_DEFAULT_LIMITS;

    limits.max_notes = 2; /* the file has three */
    TEST_ASSERT_EQUAL(MIDI_E_LIMIT, midi_parse(file.bytes, file.len, &limits, &parsed));
    limits.max_notes = 3;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, &limits, &parsed));
}

static void test_the_event_budget_is_shared_across_tracks(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    midi_limits limits = MIDI_DEFAULT_LIMITS;
    note_on(&body, 0, 0, 60, 64);
    note_off(&body, 10, 0, 60);
    header(&file, 1, 2, 96);
    track(&file, &body, 1); /* three events */
    track(&file, &body, 1); /* three more */
    limits.max_events = 5;
    TEST_ASSERT_EQUAL(MIDI_E_LIMIT, midi_parse(file.bytes, file.len, &limits, &parsed));
    limits.max_events = 6;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, &limits, &parsed));
}

static void test_too_many_chunks_is_refused(void) {
    buffer file = {{0}, 0};
    buffer body = {{0}, 0};
    const uint8_t other[] = {'X', 'X', 'X', 'X', 0, 0, 0, 0};
    midi_limits limits = MIDI_DEFAULT_LIMITS;
    int i;
    header(&file, 0, 1, 96);
    for (i = 0; i < 5; i++) {
        put(&file, other, sizeof(other)); /* five ignorable chunks before the track */
    }
    note_on(&body, 0, 0, 60, 64);
    note_off(&body, 10, 0, 60);
    track(&file, &body, 1);
    limits.max_tracks = 1; /* at most four chunks per allowed track */
    TEST_ASSERT_EQUAL(MIDI_E_LIMIT, midi_parse(file.bytes, file.len, &limits, &parsed));
    limits.max_tracks = 2;
    TEST_ASSERT_EQUAL(MIDI_OK, midi_parse(file.bytes, file.len, &limits, &parsed));
}

/* --- housekeeping --------------------------------------------------------- */

static void test_free_is_safe_and_names_exist(void) {
    midi_file empty;
    memset(&empty, 0, sizeof(empty));
    midi_free(&empty);
    midi_free(&empty);
    midi_free(NULL);
    TEST_ASSERT_EQUAL_STRING("MIDI_OK", midi_status_name(MIDI_OK));
    TEST_ASSERT_EQUAL_STRING("MIDI_E_LIMIT", midi_status_name(MIDI_E_LIMIT));
    TEST_ASSERT_EQUAL_STRING("MIDI_E_UNKNOWN", midi_status_name((midi_status)99));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_vlq_single_byte);
    RUN_TEST(test_vlq_multi_byte);
    RUN_TEST(test_vlq_refuses_five_bytes_and_a_short_buffer);
    RUN_TEST(test_a_simple_file_parses);
    RUN_TEST(test_not_a_midi_file);
    RUN_TEST(test_header_too_short);
    RUN_TEST(test_header_length_and_division_are_checked);
    RUN_TEST(test_a_longer_header_is_skipped);
    RUN_TEST(test_format_1_and_2_with_several_tracks);
    RUN_TEST(test_unknown_chunks_are_skipped_and_missing_tracks_reported);
    RUN_TEST(test_no_tracks_is_an_error);
    RUN_TEST(test_velocity_zero_closes_a_note_and_zero_length_notes_are_dropped);
    RUN_TEST(test_retriggered_pitch_closes_oldest_first);
    RUN_TEST(test_a_stray_note_off_is_ignored);
    RUN_TEST(test_notes_still_held_are_closed_at_the_end_of_the_track);
    RUN_TEST(test_running_status_reuses_the_previous_channel_status);
    RUN_TEST(test_running_status_survives_a_meta_event);
    RUN_TEST(test_a_data_byte_before_any_status_is_refused);
    RUN_TEST(test_an_unknown_status_byte_is_refused);
    RUN_TEST(test_meta_events_are_read_or_ignored_by_their_shape);
    RUN_TEST(test_long_names_are_cut_not_overflowed);
    RUN_TEST(test_end_of_track_stops_reading);
    RUN_TEST(test_sysex_controllers_programs_and_the_rest);
    RUN_TEST(test_a_cut_inside_an_event_is_an_error);
    RUN_TEST(test_a_cut_between_events_parses_what_is_there);
    RUN_TEST(test_every_prefix_of_a_file_is_handled);
    RUN_TEST(test_every_single_byte_corruption_is_handled);
    RUN_TEST(test_limits_are_enforced_and_near_misses_pass);
    RUN_TEST(test_the_event_budget_is_shared_across_tracks);
    RUN_TEST(test_too_many_chunks_is_refused);
    RUN_TEST(test_free_is_safe_and_names_exist);
    return UNITY_END();
}
