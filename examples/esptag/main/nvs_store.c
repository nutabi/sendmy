#include "nvs_store.h"

#include "common.h"  // D_LEN, SK_LEN
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

#define LOG_TAG "nvs_store"

#define NVS_NAMESPACE "esptag"
#define NVS_KEY_D0 "d_0"
#define NVS_KEY_SK0 "sk_0"

// Writable runtime-state namespace, kept separate from the read-only seed
// namespace above so the seed image (flashed from seed.csv) and the mutable
// rotation counter never share a namespace.
#define NVS_NAMESPACE_STATE "esptag_st"
#define NVS_KEY_COUNTER "counter"

/* Header implementation */

status_t nvs_store_init(void)
{
    // Any failure (including a corrupt partition) is fatal: the main task
    // aborts, and recovery means re-flashing the seed anyway.
    esp_err_t err = nvs_flash_init();
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "nvs init failed: %s", esp_err_to_name(err));
        return STATUS_ERR;
    }
    return STATUS_OK;
}

// Read a fixed-size blob key into out, failing if the key is missing or its
// stored length does not match.
static status_t load_blob(nvs_handle_t handle, const char *key, void *out, size_t expected)
{
    size_t len = expected;
    esp_err_t err = nvs_get_blob(handle, key, out, &len);
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "missing key '%s': %s", key, esp_err_to_name(err));
        return STATUS_ERR;
    }
    if (len != expected) {
        ESP_LOGE(LOG_TAG, "key '%s' size mismatch (%u != %u)", key, (unsigned)len, (unsigned)expected);
        return STATUS_ERR;
    }
    return STATUS_OK;
}

status_t nvs_store_load_seed(uint8_t d_0[D_LEN], uint8_t sk_0[SK_LEN])
{
    if (d_0 == NULL || sk_0 == NULL) {
        ESP_LOGE(LOG_TAG, "seed buffer is null");
        return STATUS_ERR;
    }

    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        // ESP_ERR_NVS_NOT_FOUND here means the namespace was never written:
        // the device has not been provisioned with a seed.
        ESP_LOGE(LOG_TAG, "nvs open failed (not provisioned?): %s", esp_err_to_name(err));
        return STATUS_ERR;
    }

    status_t ret = STATUS_OK;
    if (load_blob(handle, NVS_KEY_D0, d_0, D_LEN) != STATUS_OK ||
        load_blob(handle, NVS_KEY_SK0, sk_0, SK_LEN) != STATUS_OK) {
        ret = STATUS_ERR;
    }

    nvs_close(handle);
    return ret;
}

status_t nvs_store_load_counter(uint32_t *counter)
{
    if (counter == NULL) {
        ESP_LOGE(LOG_TAG, "counter is null");
        return STATUS_ERR;
    }

    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE_STATE, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        // Namespace never written: first boot since provisioning. Start at 0.
        *counter = 0;
        return STATUS_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "state nvs open failed: %s", esp_err_to_name(err));
        return STATUS_ERR;
    }

    err = nvs_get_u32(handle, NVS_KEY_COUNTER, counter);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        // Namespace exists but the key does not: also treat as first boot.
        *counter = 0;
        return STATUS_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "counter read failed: %s", esp_err_to_name(err));
        return STATUS_ERR;
    }
    return STATUS_OK;
}

status_t nvs_store_save_counter(uint32_t counter)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE_STATE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "state nvs open failed: %s", esp_err_to_name(err));
        return STATUS_ERR;
    }

    err = nvs_set_u32(handle, NVS_KEY_COUNTER, counter);
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "counter write failed: %s", esp_err_to_name(err));
        nvs_close(handle);
        return STATUS_ERR;
    }

    err = nvs_commit(handle);
    nvs_close(handle);
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "counter commit failed: %s", esp_err_to_name(err));
        return STATUS_ERR;
    }
    return STATUS_OK;
}
