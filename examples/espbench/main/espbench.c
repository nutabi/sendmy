#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#else
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#endif
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "sendmy_carrier.h"
#include "sendmy_link.h"

static const char *TAG = "espbench";

// Payload generation modes, matching bench_common.MODE_* on the host.
#define MODE_STATIC      0
#define MODE_INCREMENTAL 1
#define MODE_RANDOM      2

// A run command (verb + a 64-hex uid + all fields) fits well under this.
#define CMD_BUF_LEN 256

// Unilink ID for the current cell, delivered in each run command's uid= field.
// The device is flashed once and reconfigured per cell over serial, so the UID
// lives only in RAM and is overwritten by every command (no NVS provisioning).
static uint8_t s_uid[SM_CR_UID_LEN];

// One parsed cell configuration, the runtime replacement for the old
// CONFIG_ESPBENCH_* compile-time parameters.
typedef struct {
    int mode;
    uint32_t mid_base;
    uint32_t adv_interval_ms;
    uint32_t update_interval_ms;
    uint32_t static_duration_ms;
    uint32_t count;
    uint8_t payload_start;
    uint32_t seed;
    bool tx_power_set;
    int8_t tx_power_dbm;
} cell_cfg_t;

// xorshift32: a tiny, fully specified PRNG. The host harness reproduces this
// bit-for-bit in Python, so a random-mode run stays verifiable end to end. The
// state must be non-zero (the generator is stuck at zero); the command parser
// rejects a zero seed in random mode so that is guaranteed.
static inline uint32_t xorshift32(uint32_t *state)
{
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

// Derive the carrier for (uid, mid, payload) and hand it to the link layer,
// which re-advertises under the new key. Logs a stable, host-parseable line:
//   "tx mid=<u> payload=0x<xx> t=<device_ms>"
// t is milliseconds since boot from esp_timer; the host also stamps its own
// wall-clock time when it reads the line, so both a relative and an absolute
// timeline are available for the throughput calculation.
static esp_err_t send_window(uint32_t mid, uint8_t payload)
{
    uint8_t carrier[SM_CR_CARRIER_LEN];
    esp_err_t err = sm_cr_build_carrier(s_uid, mid, payload, carrier);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "build carrier mid=%lu: %s", (unsigned long)mid, esp_err_to_name(err));
        return err;
    }

    // A 28-octet carrier is exactly an OF advertising key.
    ESP_ERROR_CHECK(sm_ll_set_key(carrier));

    int64_t t_ms = esp_timer_get_time() / 1000;
    ESP_LOGI(TAG, "tx mid=%lu payload=0x%02x t=%lld", (unsigned long)mid, payload, t_ms);
    return ESP_OK;
}

// Parse a 64-hex-character uid field into 32 raw bytes.
static bool parse_hex_uid(const char *s, uint8_t out[SM_CR_UID_LEN])
{
    if (strlen(s) != (size_t)SM_CR_UID_LEN * 2) {
        return false;
    }
    for (int i = 0; i < SM_CR_UID_LEN; i++) {
        char byte_hex[3] = {s[2 * i], s[2 * i + 1], '\0'};
        char *end;
        long v = strtol(byte_hex, &end, 16);
        if (*end != '\0') {
            return false;
        }
        out[i] = (uint8_t)v;
    }
    return true;
}

// Parse a "run key=val ..." command line into cfg (and s_uid). Applies defaults
// for omitted optional fields; returns false (after logging the reason) on a
// missing required field or a malformed value. `line` is modified in place.
static bool parse_cmd(char *line, cell_cfg_t *cfg)
{
    cfg->mode = -1;
    cfg->mid_base = 0;
    cfg->adv_interval_ms = SM_LL_DEFAULT_ADV_INTERVAL_MS;
    cfg->update_interval_ms = 60000;
    cfg->static_duration_ms = 600000;
    cfg->count = 16;
    cfg->payload_start = 0;
    cfg->seed = 1;
    cfg->tx_power_set = false;
    cfg->tx_power_dbm = 0;

    bool have_uid = false, have_mode = false, have_mid = false;

    char *save;
    char *tok = strtok_r(line, " ", &save);
    if (tok == NULL || strcmp(tok, "run") != 0) {
        ESP_LOGE(TAG, "parse: expected 'run' command");
        return false;
    }

    while ((tok = strtok_r(NULL, " ", &save)) != NULL) {
        char *eq = strchr(tok, '=');
        if (eq == NULL) {
            ESP_LOGE(TAG, "parse: bad token '%s' (want key=value)", tok);
            return false;
        }
        *eq = '\0';
        const char *key = tok;
        const char *val = eq + 1;

        if (strcmp(key, "uid") == 0) {
            if (!parse_hex_uid(val, s_uid)) {
                ESP_LOGE(TAG, "parse: uid must be %d hex chars", SM_CR_UID_LEN * 2);
                return false;
            }
            have_uid = true;
        } else if (strcmp(key, "mode") == 0) {
            if (strcmp(val, "static") == 0) {
                cfg->mode = MODE_STATIC;
            } else if (strcmp(val, "incremental") == 0) {
                cfg->mode = MODE_INCREMENTAL;
            } else if (strcmp(val, "random") == 0) {
                cfg->mode = MODE_RANDOM;
            } else {
                ESP_LOGE(TAG, "parse: unknown mode '%s'", val);
                return false;
            }
            have_mode = true;
        } else if (strcmp(key, "mid") == 0) {
            cfg->mid_base = strtoul(val, NULL, 10);
            have_mid = true;
        } else if (strcmp(key, "adv_ms") == 0) {
            cfg->adv_interval_ms = strtoul(val, NULL, 10);
        } else if (strcmp(key, "upd_ms") == 0) {
            cfg->update_interval_ms = strtoul(val, NULL, 10);
        } else if (strcmp(key, "count") == 0) {
            cfg->count = strtoul(val, NULL, 10);
        } else if (strcmp(key, "dur_ms") == 0) {
            cfg->static_duration_ms = strtoul(val, NULL, 10);
        } else if (strcmp(key, "pay") == 0) {
            cfg->payload_start = (uint8_t)(strtoul(val, NULL, 10) & 0xFF);
        } else if (strcmp(key, "seed") == 0) {
            cfg->seed = strtoul(val, NULL, 10);
        } else if (strcmp(key, "txdbm") == 0) {
            cfg->tx_power_set = true;
            cfg->tx_power_dbm = (int8_t)strtol(val, NULL, 10);
        } else {
            ESP_LOGW(TAG, "parse: ignoring unknown key '%s'", key);
        }
    }

    if (!have_uid || !have_mode || !have_mid) {
        ESP_LOGE(TAG, "parse: missing required field(s): %s%s%s",
                 have_uid ? "" : "uid ", have_mode ? "" : "mode ", have_mid ? "" : "mid");
        return false;
    }
    if (cfg->mode == MODE_RANDOM && cfg->seed == 0) {
        ESP_LOGE(TAG, "parse: seed must be non-zero in random mode");
        return false;
    }
    return true;
}

// Run one cell to completion, emitting the same host-parseable markers the old
// compile-time bench_task did ("run start" / "tx ..." / "done ...").
static void run_cell(const cell_cfg_t *cfg)
{
    ESP_ERROR_CHECK(sm_ll_set_adv_interval(cfg->adv_interval_ms));
    if (cfg->tx_power_set) {
        ESP_ERROR_CHECK(sm_ll_set_tx_power(cfg->tx_power_dbm));
    }

    ESP_LOGI(TAG, "run start mode=%d adv_interval_ms=%lu mid_base=%lu", cfg->mode,
             (unsigned long)cfg->adv_interval_ms, (unsigned long)cfg->mid_base);

    if (cfg->mode == MODE_STATIC) {
        // One carrier, held for the configured duration. The payload never
        // changes and the mid never advances, so a single key radiates.
        send_window(cfg->mid_base, cfg->payload_start);
        if (cfg->static_duration_ms > 0) {
            vTaskDelay(pdMS_TO_TICKS(cfg->static_duration_ms));
            ESP_LOGI(TAG, "done count=1 mid_base=%lu", (unsigned long)cfg->mid_base);
        } else {
            // Duration 0: broadcast until the next command overwrites the key;
            // no done marker (host-driven soak).
            ESP_LOGI(TAG, "static duration=0: broadcasting until next command");
        }
        return;
    }

    // Incremental / random: count windows, one per update interval.
    uint32_t rng = cfg->seed;
    for (uint32_t i = 0; i < cfg->count; i++) {
        uint8_t payload;
        if (cfg->mode == MODE_RANDOM) {
            payload = (uint8_t)(xorshift32(&rng) & 0xFF);
        } else {  // MODE_INCREMENTAL
            payload = (uint8_t)((cfg->payload_start + i) & 0xFF);
        }

        send_window(cfg->mid_base + i, payload);

        // Hold this window. The last window is not followed by a delay here; the
        // trailing delay below gives it a full interval on air before "done".
        if (i + 1 < cfg->count) {
            vTaskDelay(pdMS_TO_TICKS(cfg->update_interval_ms));
        }
    }
    // Give the final window a full update interval on air before declaring the
    // run done, so it is not unfairly penalised relative to the others.
    vTaskDelay(pdMS_TO_TICKS(cfg->update_interval_ms));
    ESP_LOGI(TAG, "done count=%lu mid_base=%lu", (unsigned long)cfg->count,
             (unsigned long)cfg->mid_base);
}

// The single reconfigurable-beacon task: route stdio through the interrupt UART
// driver, announce readiness, then loop reading one "run" command and running
// that cell. Never terminates -- the last cell's carrier keeps advertising until
// the next command overwrites it (harmless under the disjoint mids the harness
// assigns).
static void console_task(void *arg)
{
    // Unbuffered stdio so a line is delivered the moment its newline arrives and
    // logs are not held back.
    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);

    // Route stdin through the console's interrupt-driven driver + VFS so fgets
    // blocks cleanly alongside the ESP_LOG output that shares this console. The
    // host sends '\n'-terminated lines; deliver them to the app as '\n'. The
    // driver must match the *primary* console: on the ESP32-S3's native USB that
    // is USB Serial/JTAG, not UART0 (UART0 isn't wired to the USB port).
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_driver_config_t usj_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usj_cfg.rx_buffer_size = CMD_BUF_LEN * 2;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usj_cfg));
    usb_serial_jtag_vfs_set_rx_line_endings(ESP_LINE_ENDINGS_LF);
    usb_serial_jtag_vfs_use_driver();
#else
    ESP_ERROR_CHECK(uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, CMD_BUF_LEN * 2, 0, 0, NULL, 0));
    uart_vfs_dev_port_set_rx_line_endings(CONFIG_ESP_CONSOLE_UART_NUM, ESP_LINE_ENDINGS_LF);
    uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
#endif

    // Stable marker the host waits on before sending the first command.
    ESP_LOGI(TAG, "ready");

    char line[CMD_BUF_LEN];
    for (;;) {
        if (fgets(line, sizeof(line), stdin) == NULL) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        char *nl = strchr(line, '\n');
        if (nl != NULL) {
            *nl = '\0';
        }
        if (line[0] == '\0') {
            continue;  // blank line
        }

        cell_cfg_t cfg;
        if (!parse_cmd(line, &cfg)) {
            // parse_cmd logged the reason; the host times this cell out and
            // retries it with a fresh command.
            continue;
        }
        run_cell(&cfg);
    }
}

// Called once the NimBLE host has synced and is ready to advertise.
static void on_ready(void)
{
    ESP_LOGI(TAG, "BLE host ready, starting console");
    xTaskCreate(console_task, "espbench_console", 4096, NULL, 5, NULL);
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Bring up NimBLE; on_ready fires after host sync and starts the console
    // task that drives every cell. No parameters are baked in -- they arrive per
    // cell over serial.
    ESP_ERROR_CHECK(sm_ll_init(on_ready));
}
