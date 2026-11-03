#include "unity.h"
#include "voice.h"

#include <math.h>

static voice v;

void setUp(void) {
    voice_init(&v);
}

void tearDown(void) {
}

static void test_silent_at_start(void) {
    TEST_ASSERT_EQUAL_INT(-1, voice_sounding(&v));
    TEST_ASSERT_EQUAL_UINT8(0, v.count);
}

static void test_the_highest_held_note_sounds(void) {
    TEST_ASSERT_TRUE(voice_note_on(&v, 48));
    TEST_ASSERT_EQUAL_INT(48, voice_sounding(&v));
    TEST_ASSERT_TRUE(voice_note_on(&v, 72));
    TEST_ASSERT_EQUAL_INT(72, voice_sounding(&v));
    TEST_ASSERT_TRUE(voice_note_on(&v, 60));
    TEST_ASSERT_EQUAL_INT(72, voice_sounding(&v)); /* a lower note does not interrupt the tune */
    TEST_ASSERT_TRUE(voice_note_off(&v, 72));
    TEST_ASSERT_EQUAL_INT(60, voice_sounding(&v));
    TEST_ASSERT_TRUE(voice_note_off(&v, 60));
    TEST_ASSERT_EQUAL_INT(48, voice_sounding(&v));
    TEST_ASSERT_TRUE(voice_note_off(&v, 48));
    TEST_ASSERT_EQUAL_INT(-1, voice_sounding(&v));
}

static void test_duplicates_and_bad_pitches_are_refused(void) {
    TEST_ASSERT_TRUE(voice_note_on(&v, 60));
    TEST_ASSERT_FALSE(voice_note_on(&v, 60));
    TEST_ASSERT_EQUAL_UINT8(1, v.count);
    TEST_ASSERT_FALSE(voice_note_off(&v, 61));
    TEST_ASSERT_FALSE(voice_note_on(&v, 128));
    TEST_ASSERT_FALSE(voice_note_off(&v, 200));
    TEST_ASSERT_EQUAL_UINT8(1, v.count);
    TEST_ASSERT_EQUAL_UINT32(0, voice_frequency_hz(128));
}

static void test_all_off_and_the_edges_of_the_range(void) {
    TEST_ASSERT_TRUE(voice_note_on(&v, 0));
    TEST_ASSERT_TRUE(voice_note_on(&v, 127));
    TEST_ASSERT_EQUAL_INT(127, voice_sounding(&v));
    TEST_ASSERT_TRUE(voice_note_off(&v, 127));
    TEST_ASSERT_EQUAL_INT(0, voice_sounding(&v));
    voice_all_off(&v);
    TEST_ASSERT_EQUAL_INT(-1, voice_sounding(&v));
    TEST_ASSERT_FALSE(voice_note_off(&v, 0));
}

static void test_frequencies_follow_equal_temperament(void) {
    int pitch;
    TEST_ASSERT_EQUAL_UINT32(440, voice_frequency_hz(69));
    TEST_ASSERT_EQUAL_UINT32(262, voice_frequency_hz(60));
    TEST_ASSERT_EQUAL_UINT32(28, voice_frequency_hz(21));
    TEST_ASSERT_EQUAL_UINT32(4186, voice_frequency_hz(108));
    TEST_ASSERT_EQUAL_UINT32(12544, voice_frequency_hz(127));
    for (pitch = 0; pitch < 128; pitch++) {
        double expected = 440.0 * pow(2.0, (pitch - 69) / 12.0);
        TEST_ASSERT_DOUBLE_WITHIN(0.51, expected, (double)voice_frequency_hz((uint8_t)pitch));
    }
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_silent_at_start);
    RUN_TEST(test_the_highest_held_note_sounds);
    RUN_TEST(test_duplicates_and_bad_pitches_are_refused);
    RUN_TEST(test_all_off_and_the_edges_of_the_range);
    RUN_TEST(test_frequencies_follow_equal_temperament);
    return UNITY_END();
}
