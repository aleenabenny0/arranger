/*
 * A bounded Standard MIDI File parser in portable C11.
 *
 * It mirrors the Python reader in src/arranger/io.py byte for byte: the same
 * events are kept (notes, tempo, time and key signatures, sustain pedal,
 * program changes, track names), the same malformed input is refused, and the
 * same limits apply. scripts/midi_diff.py parses files with both and compares.
 *
 * Every byte comes from a stranger. Nothing here trusts a declared length:
 * every read is bounds-checked against the buffer, a variable-length quantity
 * longer than four bytes is an error, a chunk that claims more bytes than the
 * file holds is cut at the end of the file, and the number of tracks, events
 * and notes is capped by midi_limits. No input can make the parser read
 * outside the buffer, and the only allocations are the output arrays.
 */
#ifndef ARRANGER_MIDI_H
#define ARRANGER_MIDI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Bytes of a track name or instrument name kept, raw as written in the file. */
#define MIDI_NAME_MAX 200

typedef enum midi_status {
    MIDI_OK = 0,
    MIDI_E_NOT_MIDI,           /* no MThd header */
    MIDI_E_BAD_HEADER,         /* header shorter than six bytes, or a division of zero */
    MIDI_E_UNSUPPORTED_FORMAT, /* format 3 or higher */
    MIDI_E_SMPTE,              /* SMPTE time code division; only ticks per quarter note are read */
    MIDI_E_TRUNCATED,          /* the data ends inside a header, an event or a payload */
    MIDI_E_BAD_VLQ,            /* a variable-length quantity longer than four bytes */
    MIDI_E_BAD_STATUS,         /* a data byte before any status byte, or an unknown status byte */
    MIDI_E_NO_TRACKS,          /* the file holds no MTrk chunk */
    MIDI_E_LIMIT,              /* a value in midi_limits was exceeded */
    MIDI_E_NOMEM               /* an output array could not be allocated */
} midi_status;

typedef struct midi_limits {
    size_t max_bytes;    /* the whole file */
    uint32_t max_tracks; /* declared in the header, and MTrk chunks read */
    uint32_t max_notes;  /* closed notes in one track */
    uint32_t max_events; /* events across the whole file */
} midi_limits;

/* The same numbers as arranger.limits.ImportLimits. */
extern const midi_limits MIDI_DEFAULT_LIMITS;

typedef struct midi_note {
    uint64_t start; /* absolute tick */
    uint64_t end;   /* absolute tick, always greater than start */
    uint8_t pitch;
    uint8_t channel;
    uint8_t velocity;
} midi_note;

typedef struct midi_tempo {
    uint64_t tick;
    uint32_t usec_per_quarter;
} midi_tempo;

typedef struct midi_time_signature {
    uint64_t tick;
    uint8_t numerator;
    uint8_t denominator_power; /* the denominator is 2 to this power; at most 6 */
} midi_time_signature;

typedef struct midi_key_signature {
    uint64_t tick;
    int8_t fifths; /* -7 (seven flats) to 7 (seven sharps) */
    bool minor;
} midi_key_signature;

typedef struct midi_pedal {
    uint64_t tick;
    uint8_t channel;
    bool down; /* controller 64 with a value of 64 or more */
} midi_pedal;

typedef struct midi_track {
    char name[MIDI_NAME_MAX + 1];       /* first sequence/track name meta event, raw bytes */
    size_t name_len;
    char instrument[MIDI_NAME_MAX + 1]; /* first instrument name meta event, raw bytes */
    size_t instrument_len;
    midi_note *notes;
    size_t note_count;
    midi_tempo *tempos;
    size_t tempo_count;
    midi_time_signature *time_signatures;
    size_t time_signature_count;
    midi_key_signature *key_signatures;
    size_t key_signature_count;
    midi_pedal *pedals;
    size_t pedal_count;
    int16_t programs[16]; /* the first program change on each channel, or -1 */
    uint64_t end_tick;    /* the tick of the end-of-track event, or of the last event */
    uint32_t events;      /* events read, the end-of-track event included */
    uint32_t unclosed;    /* notes still held at the end of the track, closed there */
    /* capacities of the arrays above; private */
    size_t note_cap, tempo_cap, time_signature_cap, key_signature_cap, pedal_cap;
} midi_track;

typedef struct midi_file {
    uint16_t format;
    uint16_t declared_tracks; /* from the header; fewer may be present */
    uint16_t division;        /* ticks per quarter note */
    midi_track *tracks;
    size_t track_count;
    bool truncated;           /* a chunk claimed more bytes than the file holds */
    const char *detail;       /* a static sentence about an error, or NULL */
} midi_file;

/*
 * Parse `len` bytes at `data`. `limits` may be NULL for the defaults. On
 * MIDI_OK, `out` owns arrays that midi_free releases; on any error, `out` is
 * left empty (midi_free is still safe to call) and out->detail says what was
 * wrong.
 */
midi_status midi_parse(const uint8_t *data, size_t len, const midi_limits *limits, midi_file *out);

void midi_free(midi_file *file);

const char *midi_status_name(midi_status status);

/*
 * Read one variable-length quantity at data[*pos], advancing *pos. Returns
 * MIDI_E_TRUNCATED when the data ends first and MIDI_E_BAD_VLQ when the value
 * would take more than four bytes.
 */
midi_status midi_read_vlq(const uint8_t *data, size_t len, size_t *pos, uint32_t *value);

#ifdef __cplusplus
}
#endif

#endif /* ARRANGER_MIDI_H */
