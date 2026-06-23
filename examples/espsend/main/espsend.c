#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sendmy_carrier.h"
#include "sendmy_link.h"

#include <string.h>

static const char *TAG = "espsend";

// Test payload: octet for message_id is 2^(message_id % 8), so the values walk
// a single one-hot bit (0x01, 0x02, ... 0x80) and repeat every 8 ids. The
// message_id itself is a monotonic 32-bit counter and is never wrapped here;
// only the payload cycles.
#define PAYLOAD_CYCLE 8

// How long each octet is advertised before advancing to the next message_id.
// Apple's network needs time to observe a beacon and file a location report,
// so a real deployment would use minutes here; this is a demo-friendly value.
#define OCTET_WINDOW_MS (60 * 1000)

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

// Continuously derives and advertises one carrier per octet. message_id is a
// monotonic 32-bit counter that advances forever; payload is 2^(message_id % 8).
static void sender_task(void *arg)
{
    for (uint32_t mid = 0;; mid++) {
        uint8_t payload = (uint8_t)(1u << (mid % PAYLOAD_CYCLE));
        uint8_t carrier[SM_CR_CARRIER_LEN];

        esp_err_t err = sm_cr_build_carrier(s_uid, mid, payload, carrier);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "build carrier mid=%lu: %s", (unsigned long)mid, esp_err_to_name(err));
            continue;
        }

        // A 28-octet carrier is exactly a Find My advertising key.
        ESP_ERROR_CHECK(sm_ll_set_key(carrier));
        ESP_LOGI(TAG, "tx mid=%lu payload=0x%02x", (unsigned long)mid, payload);

        vTaskDelay(pdMS_TO_TICKS(OCTET_WINDOW_MS));
    }
}

// Called once the NimBLE host has synced and is ready to advertise.
static void on_ready(void)
{
    ESP_LOGI(TAG, "BLE host ready, starting transmission");
    xTaskCreate(sender_task, "sendmy_tx", 4096, NULL, 5, NULL);
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

    // Bring up NimBLE; on_ready fires after host sync, advertising every 1000 ms.
    ESP_ERROR_CHECK(sm_ll_init(on_ready, 1000));
}
