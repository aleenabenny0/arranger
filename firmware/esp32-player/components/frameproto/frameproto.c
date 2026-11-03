#include "frameproto.h"

#include <string.h>

enum { WAIT_SYNC1, WAIT_SYNC2, WAIT_TYPE, WAIT_LENGTH, WAIT_PAYLOAD, WAIT_CRC };

/* One byte through CRC-8, polynomial 0x07, most significant bit first. */
static uint8_t crc_step(uint8_t crc, uint8_t byte) {
    int bit;
    unsigned int acc = (unsigned int)crc ^ (unsigned int)byte;
    for (bit = 0; bit < 8; bit++) {
        acc = (acc & 0x80u) ? ((acc << 1) ^ 0x07u) : (acc << 1);
        acc &= 0xFFu;
    }
    return (uint8_t)acc;
}

uint8_t frame_crc8(const uint8_t *data, size_t length) {
    uint8_t crc = 0;
    size_t i;
    for (i = 0; i < length; i++) {
        crc = crc_step(crc, data[i]);
    }
    return crc;
}

void frame_parser_init(frame_parser *parser) {
    memset(parser, 0, sizeof(*parser));
    parser->state = WAIT_SYNC1;
}

frame_event frame_parser_feed(frame_parser *parser, uint8_t byte) {
    switch (parser->state) {
    case WAIT_SYNC1:
        if (byte == FRAME_SYNC1) {
            parser->state = WAIT_SYNC2;
        } else {
            parser->resyncs++;
        }
        return FRAME_NONE;
    case WAIT_SYNC2:
        if (byte == FRAME_SYNC2) {
            parser->state = WAIT_TYPE;
        } else if (byte == FRAME_SYNC1) {
            parser->resyncs++; /* a sync1 followed by another sync1: keep the latest */
        } else {
            parser->resyncs += 2;
            parser->state = WAIT_SYNC1;
        }
        return FRAME_NONE;
    case WAIT_TYPE:
        parser->frame.type = byte;
        parser->crc = crc_step(0, byte);
        parser->state = WAIT_LENGTH;
        return FRAME_NONE;
    case WAIT_LENGTH:
        if (byte > FRAME_MAX_PAYLOAD) {
            parser->length_errors++;
            parser->state = WAIT_SYNC1;
            return FRAME_ERR_LENGTH;
        }
        parser->frame.length = byte;
        parser->crc = crc_step(parser->crc, byte);
        parser->index = 0;
        parser->state = byte ? WAIT_PAYLOAD : WAIT_CRC;
        return FRAME_NONE;
    case WAIT_PAYLOAD:
        parser->frame.payload[parser->index++] = byte;
        parser->crc = crc_step(parser->crc, byte);
        if (parser->index >= parser->frame.length) {
            parser->state = WAIT_CRC;
        }
        return FRAME_NONE;
    case WAIT_CRC:
        parser->state = WAIT_SYNC1;
        if (byte != parser->crc) {
            parser->crc_errors++;
            return FRAME_ERR_CRC;
        }
        parser->frames++;
        return FRAME_READY;
    default:
        parser->state = WAIT_SYNC1;
        return FRAME_NONE;
    }
}

size_t frame_encode(uint8_t type, const uint8_t *payload, uint8_t length, uint8_t *out, size_t out_size) {
    size_t total = FRAME_OVERHEAD + length;
    if (length > FRAME_MAX_PAYLOAD || out_size < total || (length && payload == NULL)) {
        return 0;
    }
    out[0] = FRAME_SYNC1;
    out[1] = FRAME_SYNC2;
    out[2] = type;
    out[3] = length;
    if (length) {
        memcpy(out + 4, payload, length);
    }
    out[4 + length] = frame_crc8(out + 2, (size_t)length + 2u);
    return total;
}
