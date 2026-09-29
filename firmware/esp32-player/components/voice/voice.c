#include "voice.h"

#include <string.h>

/* round(440 * 2^((pitch - 69) / 12)) for every MIDI pitch; checked by the host tests. */
static const uint16_t FREQUENCY_HZ[128] = {
    8,    9,    9,    10,   10,   11,   12,   12,   13,   14,   15,   15,   16,   17,   18,    19,
    21,   22,   23,   24,   26,   28,   29,   31,   33,   35,   37,   39,   41,   44,   46,    49,
    52,   55,   58,   62,   65,   69,   73,   78,   82,   87,   92,   98,   104,  110,  117,   123,
    131,  139,  147,  156,  165,  175,  185,  196,  208,  220,  233,  247,  262,  277,  294,   311,
    330,  349,  370,  392,  415,  440,  466,  494,  523,  554,  587,  622,  659,  698,  740,   784,
    831,  880,  932,  988,  1047, 1109, 1175, 1245, 1319, 1397, 1480, 1568, 1661, 1760, 1865,  1976,
    2093, 2217, 2349, 2489, 2637, 2794, 2960, 3136, 3322, 3520, 3729, 3951, 4186, 4435, 4699,  4978,
    5274, 5588, 5920, 6272, 6645, 7040, 7459, 7902, 8372, 8870, 9397, 9956, 10548, 11175, 11840, 12544,
};

void voice_init(voice *v) {
    memset(v, 0, sizeof(*v));
}

bool voice_note_on(voice *v, uint8_t pitch) {
    uint8_t bit;
    if (pitch > 127) {
        return false;
    }
    bit = (uint8_t)(1u << (pitch & 7u));
    if (v->held[pitch >> 3] & bit) {
        return false;
    }
    v->held[pitch >> 3] |= bit;
    v->count++;
    return true;
}

bool voice_note_off(voice *v, uint8_t pitch) {
    uint8_t bit;
    if (pitch > 127) {
        return false;
    }
    bit = (uint8_t)(1u << (pitch & 7u));
    if (!(v->held[pitch >> 3] & bit)) {
        return false;
    }
    v->held[pitch >> 3] = (uint8_t)(v->held[pitch >> 3] & (uint8_t)~bit);
    v->count--;
    return true;
}

void voice_all_off(voice *v) {
    memset(v->held, 0, sizeof(v->held));
    v->count = 0;
}

int voice_sounding(const voice *v) {
    int pitch;
    if (v->count == 0) {
        return -1;
    }
    for (pitch = 127; pitch >= 0; pitch--) {
        if (v->held[pitch >> 3] & (uint8_t)(1u << (pitch & 7))) {
            return pitch;
        }
    }
    return -1;
}

uint32_t voice_frequency_hz(uint8_t pitch) {
    return pitch > 127 ? 0u : FREQUENCY_HZ[pitch];
}
