#include "ble_adv.h"
#include "crypto.h"
#include "esp_log.h"
#include "nvs_store.h"
#include "sdkconfig.h"
#include "tag.h"

#include <stdlib.h>

#define LOG_TAG "main"

// File-scope so the struct outlives app_main: the BLE host task runs
// asynchronously and retains a pointer to it (see ble_adv_init).
static tag_t tag;

// Our own modules; the default level (WARN, per sdkconfig) is overridden with
// CONFIG_ESPTAG_OWN_LOG_LEVEL (INFO by default, see Kconfig.projbuild) for these
// so the rest of the system stays quiet while we control our own verbosity.
// Keep in sync with each module's LOG_TAG.
static const char *const OWN_LOG_TAGS[] = {
    "main", "crypto", "nvs_store", "tag", "ble_adv", "sendmy_link",
};

void app_main(void)
{
    for (size_t i = 0; i < sizeof(OWN_LOG_TAGS) / sizeof(OWN_LOG_TAGS[0]); i++) {
        esp_log_level_set(OWN_LOG_TAGS[i], (esp_log_level_t)CONFIG_ESPTAG_OWN_LOG_LEVEL);
    }

    if (crypto_init() != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "crypto init failed");
        abort();
    }

    if (nvs_store_init() != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "nvs init failed");
        abort();
    }

    // Load the provisioned seed; refuse to run unprovisioned.
    if (nvs_store_load_seed(tag.d_0, tag.sk_0) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "no provisioned seed; halting");
        abort();
    }

    // Resume from the persisted rotation counter so identifiers do not replay
    // across reboots. With persistence disabled (testing), always start at 0.
    uint32_t counter = 0;
#ifdef CONFIG_ESPTAG_PERSIST_COUNTER
    if (nvs_store_load_counter(&counter) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "counter load failed");
        abort();
    }
#endif  // CONFIG_ESPTAG_PERSIST_COUNTER

    if (tag_init(&tag, counter) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "tag init failed");
        tag_destroy(&tag);
        abort();
    }

    ESP_LOGI(LOG_TAG, "tag provisioned, counter=%lu", (unsigned long)tag.counter);

    // Bring up BLE; the host task takes over from here and keeps the tag alive.
    if (ble_adv_init(&tag) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "ble init failed");
        tag_destroy(&tag);
        abort();
    }
}
