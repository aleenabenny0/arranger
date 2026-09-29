/*
 * Which note a one-voice sounder plays when several are held. A piezo can
 * make one pitch at a time; the highest held note wins, which keeps the tune
 * when a bass note is under it. Plain C, compiled into the firmware and the
 * host tests alike.
 */
#ifndef ARRANGER_VOICE_H
#define ARRANGER_VOICE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct voice {
    uint8_t held[16]; /* one bit per MIDI pitch */
    uint8_t count;
} voice;

void voice_init(voice *v);

/* Returns false for a pitch over 127 or a note already held. */
bool voice_note_on(voice *v, uint8_t pitch);

/* Returns false for a pitch over 127 or a note not held. */
bool voice_note_off(voice *v, uint8_t pitch);

void voice_all_off(voice *v);

/* The pitch that should sound now: the highest held, or -1 for silence. */
int voice_sounding(const voice *v);

/* The frequency of a MIDI pitch in hertz, rounded (A4 = 440). */
uint32_t voice_frequency_hz(uint8_t pitch);

#ifdef __cplusplus
}
#endif

#endif /* ARRANGER_VOICE_H */
