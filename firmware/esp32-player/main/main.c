/*
 * Arranger player: a one-voice sounder on a piezo, driven over UART.
 *
 * The host (scripts/send_to_esp32.py) keeps time and sends NOTE_ON and
 * NOTE_OFF frames as the piece plays. This firmware keeps the set of held
 * notes, sounds the highest one on a PWM channel, answers every frame with an
 * ACK, and releases everything if the host goes quiet. The frame parser and
 * the note logic are plain C in components/, tested on the host; this file is
 * only the wiring to ESP-IDF's UART, LEDC and GPIO drivers.
 */
#include <stdint.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "frameproto.h"
#include "voice.h"

#define FIRMWARE_MAJOR 0u
#define FIRMWARE_MINOR 1u

#define UART_PORT ((uart_port_t)CONFIG_PLAYER_UART_NUM)
#define LED_GPIO ((gpio_num_t)CONFIG_PLAYER_LED_GPIO)
#define SOUNDER_MODE LEDC_LOW_SPEED_MODE
#define SOUNDER_TIMER LEDC_TIMER_0
#define SOUNDER_CHANNEL LEDC_CHANNEL_0
#define SOUNDER_DUTY 512u /* half of a 10-bit period: a square wave */
#define MIN_FREQUENCY_HZ 20u

static const char *TAG = "player";

static voice held;
static int sounding = -1;
static int64_t last_frame_us;
static uint8_t tx[FRAME_OVERHEAD + FRAME_MAX_PAYLOAD];

static void sounder_init(void) {
    ledc_timer_config_t timer = {
        .speed_mode = SOUNDER_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = SOUNDER_TIMER,
        .freq_hz = 440,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_channel_config_t channel = {
        .gpio_num = CONFIG_PLAYER_PIEZO_GPIO,
        .speed_mode = SOUNDER_MODE,
        .channel = SOUNDER_CHANNEL,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = SOUNDER_TIMER,
        .duty = 0,
        .hpoint = 0,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));
    ESP_ERROR_CHECK(ledc_channel_config(&channel));
}

/* Make the output match the held notes: the highest sounds, silence otherwise. */
static void sounder_apply(void) {
    int next = voice_sounding(&held);
    if (next == sounding) {
        return;
    }
    sounding = next;
    if (next < 0) {
        ledc_set_duty(SOUNDER_MODE, SOUNDER_CHANNEL, 0);
        ledc_update_duty(SOUNDER_MODE, SOUNDER_CHANNEL);
        gpio_set_level(LED_GPIO, 0);
        return;
    }
    uint32_t hz = voice_frequency_hz((uint8_t)next);
    if (hz < MIN_FREQUENCY_HZ) {
        hz = MIN_FREQUENCY_HZ;
    }
    ledc_set_freq(SOUNDER_MODE, SOUNDER_TIMER, hz);
    ledc_set_duty(SOUNDER_MODE, SOUNDER_CHANNEL, SOUNDER_DUTY);
    ledc_update_duty(SOUNDER_MODE, SOUNDER_CHANNEL);
    gpio_set_level(LED_GPIO, 1);
}

static void send_frame(uint8_t type, const uint8_t *payload, uint8_t length) {
    size_t n = frame_encode(type, payload, length, tx, sizeof(tx));
    if (n) {
        uart_write_bytes(UART_PORT, (const char *)tx, n);
    }
}

static void ack(uint8_t type, uint8_t status) {
    uint8_t payload[2] = {type, status};
    send_frame(FRAME_ACK, payload, 2);
}

static void handle(const frame *f) {
    switch (f->type) {
    case FRAME_NOTE_ON:
        if (f->length != 2 || f->payload[0] > 127) {
            ack(f->type, FRAME_STATUS_BAD_PAYLOAD);
            return;
        }
        if (f->payload[1] == 0) { /* velocity zero is a note-off, as in MIDI */
            voice_note_off(&held, f->payload[0]);
        } else {
            voice_note_on(&held, f->payload[0]);
        }
        sounder_apply();
        ack(f->type, FRAME_STATUS_OK);
        return;
    case FRAME_NOTE_OFF:
        if (f->length != 1 || f->payload[0] > 127) {
            ack(f->type, FRAME_STATUS_BAD_PAYLOAD);
            return;
        }
        voice_note_off(&held, f->payload[0]);
        sounder_apply();
        ack(f->type, FRAME_STATUS_OK);
        return;
    case FRAME_ALL_OFF:
        voice_all_off(&held);
        sounder_apply();
        ack(f->type, FRAME_STATUS_OK);
        return;
    case FRAME_PING: {
        uint8_t payload[3] = {FRAME_PROTOCOL_VERSION, FIRMWARE_MAJOR, FIRMWARE_MINOR};
        send_frame(FRAME_PONG, payload, 3);
        return;
    }
    case FRAME_TONE_TEST: {
        /* A wiring check: one note for a while, then silence. */
        uint32_t duration_ms;
        if (f->length != 3 || f->payload[0] > 127) {
            ack(f->type, FRAME_STATUS_BAD_PAYLOAD);
            return;
        }
        duration_ms = (uint32_t)f->payload[1] | ((uint32_t)f->payload[2] << 8);
        voice_all_off(&held);
        voice_note_on(&held, f->payload[0]);
        sounder_apply();
        ack(f->type, FRAME_STATUS_OK);
        vTaskDelay(pdMS_TO_TICKS(duration_ms));
        voice_all_off(&held);
        sounder_apply();
        return;
    }
    default:
        ack(f->type, FRAME_STATUS_UNKNOWN_TYPE);
        return;
    }
}

static void rx_task(void *arg) {
    frame_parser parser;
    uint8_t buffer[128];
    (void)arg;
    frame_parser_init(&parser);
    for (;;) {
        int n = uart_read_bytes(UART_PORT, buffer, sizeof(buffer), pdMS_TO_TICKS(50));
        int i;
        for (i = 0; i < n; i++) {
            frame_event event = frame_parser_feed(&parser, buffer[i]);
            if (event == FRAME_READY) {
                last_frame_us = esp_timer_get_time();
                handle(&parser.frame);
            } else if (event == FRAME_ERR_CRC || event == FRAME_ERR_LENGTH) {
                uint8_t code = event == FRAME_ERR_CRC ? FRAME_ERROR_CRC : FRAME_ERROR_LENGTH;
                send_frame(FRAME_ERROR, &code, 1);
            }
        }
        /* The host stopped talking while notes were held: release them. */
        if (held.count && esp_timer_get_time() - last_frame_us > (int64_t)CONFIG_PLAYER_IDLE_SILENCE_MS * 1000) {
            ESP_LOGW(TAG, "no frame for %d ms: releasing %u held note(s)", CONFIG_PLAYER_IDLE_SILENCE_MS, (unsigned)held.count);
            voice_all_off(&held);
            sounder_apply();
        }
    }
}

void app_main(void) {
    uart_config_t config = {
        .baud_rate = CONFIG_PLAYER_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(UART_PORT, 2048, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_PORT, &config));
    ESP_ERROR_CHECK(uart_set_pin(UART_PORT, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(LED_GPIO, 0);

    voice_init(&held);
    sounder_init();
    last_frame_us = esp_timer_get_time();

    xTaskCreate(rx_task, "player_rx", 4096, NULL, 10, NULL);
    ESP_LOGI(TAG, "ready: piezo on GPIO %d, LED on GPIO %d, UART%d at %d baud, protocol %u",
             CONFIG_PLAYER_PIEZO_GPIO, CONFIG_PLAYER_LED_GPIO, CONFIG_PLAYER_UART_NUM, CONFIG_PLAYER_UART_BAUD,
             (unsigned)FRAME_PROTOCOL_VERSION);
}
