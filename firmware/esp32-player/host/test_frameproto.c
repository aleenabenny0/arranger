/* The frame protocol, byte by byte. The golden bytes here are the same ones
 * tests/test_esp32_sender.py checks on the Python side. */
#include "frameproto.h"
#include "unity.h"

#include <string.h>

static frame_parser parser;

void setUp(void) {
    frame_parser_init(&parser);
}

void tearDown(void) {
}

/* Feed bytes; returns the last event and counts the frames delivered. */
static frame_event feed_all(const uint8_t *bytes, size_t n, int *ready) {
    frame_event last = FRAME_NONE;
    size_t i;
    *ready = 0;
    for (i = 0; i < n; i++) {
        last = frame_parser_feed(&parser, bytes[i]);
        if (last == FRAME_READY) {
            (*ready)++;
        }
    }
    return last;
}

static void test_crc8_check_value(void) {
    const uint8_t check[] = "123456789";
    TEST_ASSERT_EQUAL_HEX8(0xF4, frame_crc8(check, 9));
    TEST_ASSERT_EQUAL_HEX8(0x00, frame_crc8(check, 0));
}

static void test_encode_note_on_matches_the_golden_bytes(void) {
    const uint8_t payload[] = {60, 100};
    const uint8_t expected[] = {0xA5, 0x5A, 0x01, 0x02, 60, 100, 0xFE};
    uint8_t out[16];
    size_t n = frame_encode(FRAME_NOTE_ON, payload, 2, out, sizeof(out));
    TEST_ASSERT_EQUAL_size_t(sizeof(expected), n);
    TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, out, sizeof(expected));
}

static void test_encode_empty_payload_and_limits(void) {
    const uint8_t expected_ping[] = {0xA5, 0x5A, 0x04, 0x00, 0x54};
    uint8_t out[FRAME_OVERHEAD + FRAME_MAX_PAYLOAD];
    uint8_t big[FRAME_MAX_PAYLOAD + 1];
    memset(big, 0x11, sizeof(big));
    TEST_ASSERT_EQUAL_size_t(5, frame_encode(FRAME_PING, NULL, 0, out, sizeof(out)));
    TEST_ASSERT_EQUAL_HEX8_ARRAY(expected_ping, out, 5);
    TEST_ASSERT_EQUAL_size_t(FRAME_OVERHEAD + FRAME_MAX_PAYLOAD, frame_encode(0x10, big, FRAME_MAX_PAYLOAD, out, sizeof(out)));
    TEST_ASSERT_EQUAL_size_t(0, frame_encode(0x10, big, FRAME_MAX_PAYLOAD + 1, out, sizeof(out))); /* too long */
    TEST_ASSERT_EQUAL_size_t(0, frame_encode(0x10, big, 4, out, 8));                                 /* too small a buffer */
    TEST_ASSERT_EQUAL_size_t(0, frame_encode(0x10, NULL, 4, out, sizeof(out)));                      /* no payload given */
}

static void test_parse_one_frame_byte_by_byte(void) {
    const uint8_t bytes[] = {0xA5, 0x5A, 0x01, 0x02, 60, 100, 0xFE};
    int ready;
    TEST_ASSERT_EQUAL(FRAME_READY, feed_all(bytes, sizeof(bytes), &ready));
    TEST_ASSERT_EQUAL_INT(1, ready);
    TEST_ASSERT_EQUAL_HEX8(FRAME_NOTE_ON, parser.frame.type);
    TEST_ASSERT_EQUAL_UINT8(2, parser.frame.length);
    TEST_ASSERT_EQUAL_UINT8(60, parser.frame.payload[0]);
    TEST_ASSERT_EQUAL_UINT8(100, parser.frame.payload[1]);
    TEST_ASSERT_EQUAL_UINT32(1, parser.frames);
}

static void test_round_trip_every_payload_length(void) {
    uint8_t payload[FRAME_MAX_PAYLOAD];
    uint8_t out[FRAME_OVERHEAD + FRAME_MAX_PAYLOAD];
    uint8_t length;
    for (length = 0; length <= FRAME_MAX_PAYLOAD; length++) {
        size_t n;
        int ready;
        uint8_t i;
        for (i = 0; i < length; i++) {
            payload[i] = (uint8_t)(i * 7u + length);
        }
        n = frame_encode(0x42, payload, length, out, sizeof(out));
        TEST_ASSERT_EQUAL_size_t(FRAME_OVERHEAD + length, n);
        TEST_ASSERT_EQUAL(FRAME_READY, feed_all(out, n, &ready));
        TEST_ASSERT_EQUAL_UINT8(length, parser.frame.length);
        if (length) {
            TEST_ASSERT_EQUAL_HEX8_ARRAY(payload, parser.frame.payload, length);
        }
    }
}

static void test_garbage_before_and_between_frames_is_skipped(void) {
    const uint8_t bytes[] = {
        'b', 'o', 'o', 't', ' ', 0xA5, 'x',          /* text, and a lone sync byte */
        0xA5, 0x5A, 0x04, 0x00, 0x54,                 /* PING */
        0x00, 0xFF, 0xA5, 0xA5, 0x5A, 0x03, 0x00, 0x3F, /* noise, a doubled sync1, then ALL_OFF */
    };
    int ready;
    feed_all(bytes, sizeof(bytes), &ready);
    TEST_ASSERT_EQUAL_INT(2, ready);
    TEST_ASSERT_EQUAL_HEX8(FRAME_ALL_OFF, parser.frame.type);
    TEST_ASSERT_TRUE(parser.resyncs > 0);
}

static void test_a_bad_crc_drops_the_frame_and_the_next_one_still_parses(void) {
    const uint8_t bad[] = {0xA5, 0x5A, 0x01, 0x02, 60, 100, 0xFF};
    const uint8_t good[] = {0xA5, 0x5A, 0x02, 0x01, 60, 0x77};
    int ready;
    TEST_ASSERT_EQUAL(FRAME_ERR_CRC, feed_all(bad, sizeof(bad), &ready));
    TEST_ASSERT_EQUAL_INT(0, ready);
    TEST_ASSERT_EQUAL_UINT32(1, parser.crc_errors);
    TEST_ASSERT_EQUAL(FRAME_READY, feed_all(good, sizeof(good), &ready));
    TEST_ASSERT_EQUAL_HEX8(FRAME_NOTE_OFF, parser.frame.type);
    TEST_ASSERT_EQUAL_UINT8(60, parser.frame.payload[0]);
}

static void test_an_oversized_length_is_refused_before_any_payload(void) {
    const uint8_t bytes[] = {0xA5, 0x5A, 0x01, (uint8_t)(FRAME_MAX_PAYLOAD + 1)};
    const uint8_t next[] = {0xA5, 0x5A, 0x04, 0x00, 0x54};
    int ready;
    TEST_ASSERT_EQUAL(FRAME_ERR_LENGTH, feed_all(bytes, sizeof(bytes), &ready));
    TEST_ASSERT_EQUAL_UINT32(1, parser.length_errors);
    TEST_ASSERT_EQUAL(FRAME_READY, feed_all(next, sizeof(next), &ready));
    TEST_ASSERT_EQUAL_HEX8(FRAME_PING, parser.frame.type);
}

static void test_back_to_back_frames_in_one_buffer(void) {
    const uint8_t bytes[] = {
        0xA5, 0x5A, 0x01, 0x02, 60, 100, 0xFE,
        0xA5, 0x5A, 0x02, 0x01, 60, 0x77,
        0xA5, 0x5A, 0x04, 0x00, 0x54,
    };
    int ready;
    feed_all(bytes, sizeof(bytes), &ready);
    TEST_ASSERT_EQUAL_INT(3, ready);
    TEST_ASSERT_EQUAL_UINT32(3, parser.frames);
    TEST_ASSERT_EQUAL_UINT32(0, parser.crc_errors + parser.length_errors + parser.resyncs);
}

static void test_a_stream_cut_mid_frame_recovers_on_the_next_sync(void) {
    const uint8_t cut[] = {0xA5, 0x5A, 0x01, 0x02, 60}; /* the cable drops here */
    const uint8_t next[] = {0xA5, 0x5A, 0x04, 0x00, 0x54};
    int ready;
    feed_all(cut, sizeof(cut), &ready);
    TEST_ASSERT_EQUAL_INT(0, ready);
    /* The next frame's sync bytes are read as the missing payload byte and CRC:
     * that frame is lost, but the one after parses. */
    feed_all(next, sizeof(next), &ready);
    TEST_ASSERT_EQUAL_INT(0, ready);
    feed_all(next, sizeof(next), &ready);
    TEST_ASSERT_EQUAL_INT(1, ready);
    TEST_ASSERT_EQUAL_HEX8(FRAME_PING, parser.frame.type);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_crc8_check_value);
    RUN_TEST(test_encode_note_on_matches_the_golden_bytes);
    RUN_TEST(test_encode_empty_payload_and_limits);
    RUN_TEST(test_parse_one_frame_byte_by_byte);
    RUN_TEST(test_round_trip_every_payload_length);
    RUN_TEST(test_garbage_before_and_between_frames_is_skipped);
    RUN_TEST(test_a_bad_crc_drops_the_frame_and_the_next_one_still_parses);
    RUN_TEST(test_an_oversized_length_is_refused_before_any_payload);
    RUN_TEST(test_back_to_back_frames_in_one_buffer);
    RUN_TEST(test_a_stream_cut_mid_frame_recovers_on_the_next_sync);
    return UNITY_END();
}
