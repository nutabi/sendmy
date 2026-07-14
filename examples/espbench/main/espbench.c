#include <stdint.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "sendmy_carrier.h"
#include "sendmy_link.h"

static const char *TAG = "espbench";

// Payload generation modes, mirroring the ESPBENCH_MODE_* choice in Kconfig.
#define MODE_STATIC      0
#define MODE_INCREMENTAL 1
#define MODE_RANDOM      2

// The PRNG seed only exists in Kconfig when random mode is selected (it depends
// on ESPBENCH_MODE_RANDOM), but the non-static code path references it in all
// rotating modes. Supply a harmless fallback for the incremental build.
#ifndef CONFIG_ESPBENCH_RANDOM_SEED
#define CONFIG_ESPBENCH_RANDOM_SEED 1
#endif

// Unilink ID, shared out of band with the receiver. Provisioned into NVS
// (namespace "sendmy", key "uid", 32 raw bytes) by the project's NVS scripts.
static uint8_t s_uid[SM_CR_UID_LEN];

// Load the Unilink ID from the "sendmy" NVS namespace into out.
static esp_err_t load_uid(uint8_t out[SM_CR_UID_LEN])
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("sendmy", NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open(sendmy): %s", esp_err_to_name(err));
        return err;
    }

    size_t len = SM_CR_UID_LEN;
    err = nvs_get_blob(handle, "uid", out, &len);
    nvs_close(handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_get_blob(uid): %s", esp_err_to_name(err));
        return err;
    }
    if (len != SM_CR_UID_LEN) {
        ESP_LOGE(TAG, "uid blob is %u bytes, expected %u", (unsigned)len, SM_CR_UID_LEN);
        return ESP_ERR_INVALID_SIZE;
    }
    return ESP_OK;
}

// xorshift32: a tiny, fully specified PRNG. The host harness reproduces this
// bit-for-bit in Python, so a random-mode run stays verifiable end to end. The
// state must be non-zero (the generator is stuck at zero); Kconfig pins the
// seed range to [1, 0xFFFFFFFF] so that is guaranteed.
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

// Drives the whole experiment run according to the compile-time configuration,
// then prints a "done" marker the host waits on before it starts fetching.
static void bench_task(void *arg)
{
    const int mode = CONFIG_ESPBENCH_MODE;
    const uint32_t base = (uint32_t)CONFIG_ESPBENCH_MID_BASE;

    ESP_LOGI(TAG, "run start mode=%d adv_interval_ms=%d mid_base=%lu", mode,
             CONFIG_ESPBENCH_ADV_INTERVAL_MS, (unsigned long)base);

#if CONFIG_ESPBENCH_MODE_STATIC
    // One carrier, held for the configured duration. The payload never changes
    // and the mid never advances, so a single key radiates for the whole run.
    send_window(base, (uint8_t)CONFIG_ESPBENCH_PAYLOAD_START);

    if (CONFIG_ESPBENCH_STATIC_DURATION_MS > 0) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_ESPBENCH_STATIC_DURATION_MS));
        ESP_LOGI(TAG, "done count=1 mid_base=%lu", (unsigned long)base);
    } else {
        // Duration 0: broadcast forever, never emit a done marker.
        ESP_LOGI(TAG, "static duration=0: broadcasting indefinitely");
    }
#else
    // Incremental / random: MSG_COUNT windows, one per update interval.
    uint32_t rng = (uint32_t)CONFIG_ESPBENCH_RANDOM_SEED;
    for (uint32_t i = 0; i < (uint32_t)CONFIG_ESPBENCH_MSG_COUNT; i++) {
        uint8_t payload;
        if (mode == MODE_RANDOM) {
            payload = (uint8_t)(xorshift32(&rng) & 0xFF);
        } else {  // MODE_INCREMENTAL
            payload = (uint8_t)((CONFIG_ESPBENCH_PAYLOAD_START + i) & 0xFF);
        }

        send_window(base + i, payload);

        // Hold this window. The last window is not followed by a delay: the
        // done marker fires immediately, and its carrier keeps advertising until
        // reboot (harmless, and noted in the README).
        if (i + 1 < (uint32_t)CONFIG_ESPBENCH_MSG_COUNT) {
            vTaskDelay(pdMS_TO_TICKS(CONFIG_ESPBENCH_UPDATE_INTERVAL_MS));
        }
    }
    // Give the final window a full update interval on air before declaring the
    // run done, so it is not unfairly penalised relative to the others.
    vTaskDelay(pdMS_TO_TICKS(CONFIG_ESPBENCH_UPDATE_INTERVAL_MS));
    ESP_LOGI(TAG, "done count=%d mid_base=%lu", CONFIG_ESPBENCH_MSG_COUNT,
             (unsigned long)base);
#endif

    // Nothing left to do; the last key keeps advertising until reboot.
    vTaskDelete(NULL);
}

// Called once the NimBLE host has synced and is ready to advertise.
static void on_ready(void)
{
    ESP_LOGI(TAG, "BLE host ready, starting experiment run");
    xTaskCreate(bench_task, "espbench", 4096, NULL, 5, NULL);
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    if (load_uid(s_uid) != ESP_OK) {
        ESP_LOGE(TAG, "no UID provisioned; flash the sendmy NVS partition first");
        return;  // nothing to send without a UID
    }

    // Bring up NimBLE; on_ready fires after host sync. The advertising interval
    // is the key independent variable, so it comes straight from Kconfig.
    ESP_ERROR_CHECK(sm_ll_init(on_ready, CONFIG_ESPBENCH_ADV_INTERVAL_MS));
}
