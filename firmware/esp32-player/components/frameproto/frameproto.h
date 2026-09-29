/*
 * The framed serial protocol between the host (scripts/send_to_esp32.py) and
 * the player. Plain C with no dependencies, so the same file is compiled into
 * the firmware and into the host tests.
 *
 * Frame layout, host to device and device to host alike:
 *
 *   0xA5 0x5A | type | length | payload[length] | crc8
 *
 * `length` is 0 to FRAME_MAX_PAYLOAD. The CRC is CRC-8 (polynomial 0x07,
 * initial value 0, no reflection, no final XOR; the check value for
 * "123456789" is 0xF4) over type, length and payload. A receiver that sees a
 * bad sync byte, a length over the maximum or a wrong CRC drops back to
 * waiting for sync, so the stream recovers after noise or a cut cable.
 */
#ifndef ARRANGER_FRAMEPROTO_H
#define ARRANGER_FRAMEPROTO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FRAME_SYNC1 0xA5u
#define FRAME_SYNC2 0x5Au
#define FRAME_MAX_PAYLOAD 32u
#define FRAME_OVERHEAD 5u /* sync, sync, type, length, crc */

/* Host to device. */
#define FRAME_NOTE_ON 0x01u   /* payload: pitch, velocity */
#define FRAME_NOTE_OFF 0x02u  /* payload: pitch */
#define FRAME_ALL_OFF 0x03u   /* payload: none */
#define FRAME_PING 0x04u      /* payload: none */
#define FRAME_TONE_TEST 0x05u /* payload: pitch, duration_ms low byte, duration_ms high byte */

/* Device to host. */
#define FRAME_ACK 0x81u   /* payload: the type acknowledged, status (0 = done) */
#define FRAME_PONG 0x84u  /* payload: protocol version, firmware major, firmware minor */
#define FRAME_ERROR 0x7Fu /* payload: error code */

#define FRAME_PROTOCOL_VERSION 1u

/* Status bytes in an ACK, and error codes in an ERROR frame. */
#define FRAME_STATUS_OK 0u
#define FRAME_STATUS_BAD_PAYLOAD 1u /* wrong length or a value out of range */
#define FRAME_STATUS_UNKNOWN_TYPE 2u
#define FRAME_ERROR_CRC 1u
#define FRAME_ERROR_LENGTH 2u

typedef struct frame {
    uint8_t type;
    uint8_t length;
    uint8_t payload[FRAME_MAX_PAYLOAD];
} frame;

typedef enum frame_event {
    FRAME_NONE = 0,   /* the byte was consumed; nothing complete yet */
    FRAME_READY,      /* parser->frame holds a complete, checked frame */
    FRAME_ERR_LENGTH, /* a length over FRAME_MAX_PAYLOAD; the frame was dropped */
    FRAME_ERR_CRC     /* the CRC did not match; the frame was dropped */
} frame_event;

typedef struct frame_parser {
    uint8_t state;
    uint8_t index;
    uint8_t crc;
    frame frame;
    uint32_t frames;        /* complete frames delivered */
    uint32_t crc_errors;
    uint32_t length_errors;
    uint32_t resyncs;       /* bytes skipped while waiting for sync */
} frame_parser;

void frame_parser_init(frame_parser *parser);

/* Feed one received byte. On FRAME_READY, read parser->frame before feeding more. */
frame_event frame_parser_feed(frame_parser *parser, uint8_t byte);

/*
 * Write a frame into `out`. Returns the number of bytes written, or 0 when
 * the payload is too long or `out` is too small (FRAME_OVERHEAD + length).
 */
size_t frame_encode(uint8_t type, const uint8_t *payload, uint8_t length, uint8_t *out, size_t out_size);

uint8_t frame_crc8(const uint8_t *data, size_t length);

#ifdef __cplusplus
}
#endif

#endif /* ARRANGER_FRAMEPROTO_H */
