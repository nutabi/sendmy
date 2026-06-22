#include "ble_adv.h"

#include "sendmy_link.h"
#include "nvs_store.h"
#include "tag.h"

#include "esp_log.h"
#include "sdkconfig.h"

#include "nimble/nimble_npl.h"
#include "nimble/nimble_port.h"

#define LOG_TAG "ble_adv"

// Epoch length: how often the tag rotates and the advertised identifier
// changes. Configured via CONFIG_ESPTAG_ROTATE_INTERVAL_MS (esptag
// configuration menu, default 15 min to match the Find My cadence); drop it to
// a few seconds to observe rotation while testing.
#define ROTATE_INTERVAL_MS CONFIG_ESPTAG_ROTATE_INTERVAL_MS

// Tag whose rotating advertising key (p_curr) drives the broadcast. Retained
// from ble_adv_init; after init it is owned solely by the NimBLE host task
// (on_ready and the rotation callout both run there), so it needs no locking.
static tag_t *s_tag = NULL;

// Fires on the host task every ROTATE_INTERVAL_MS to advance the epoch.
static struct ble_npl_callout s_rotate_timer;

/* Static helper declaration */

// sendmy_link ready callback (runs once on the host task after sync): publishes
// the initial key and, when rotation is enabled, arms the rotation timer.
static void on_ready(void);

// Rotation timer callback (runs on the host task): advance the epoch, persist
// the counter, re-broadcast under the new key, then re-arm the timer.
static void on_rotate(struct ble_npl_event *ev);

/* Header implementation */

status_t ble_adv_init(tag_t *tag) {
    if (tag == NULL) {
        ESP_LOGE(LOG_TAG, "tag is null");
        return STATUS_ERR;
    }
    s_tag = tag;

#ifdef CONFIG_ESPTAG_ROTATE_ENABLE
    ESP_LOGI(LOG_TAG, "initialising BLE advertising (rotate every %d ms)",
             ROTATE_INTERVAL_MS);
#else
    ESP_LOGI(LOG_TAG, "initialising BLE advertising (rotation disabled)");
#endif // CONFIG_ESPTAG_ROTATE_ENABLE

    // sendmy_link is a standalone component returning esp_err_t; collapse it to
    // the firmware's status_t convention at this boundary.
    return sm_ll_init(on_ready, CONFIG_ESPTAG_ADV_INTERVAL_MS) == ESP_OK
               ? STATUS_OK
               : STATUS_ERR;
}

/* Static helper implementation */

static void on_ready(void) {
    // Publish the boot-epoch identity. sendmy_link advertises immediately since
    // the stack has synced by the time on_ready fires.
    if (sm_ll_set_key(s_tag->p_curr) != ESP_OK) {
        ESP_LOGE(LOG_TAG, "initial key set failed");
        return;
    }

#ifdef CONFIG_ESPTAG_ROTATE_ENABLE
    // on_ready fires exactly once, so the callout is initialised here (the
    // default eventq exists after nimble_port_init) without re-initing on a
    // re-sync. The callout fires on the host task, so on_rotate is free to call
    // sm_ll_set_key directly.
    ble_npl_callout_init(&s_rotate_timer, nimble_port_get_dflt_eventq(),
                         on_rotate, NULL);
    ble_npl_callout_reset(&s_rotate_timer,
                          ble_npl_time_ms_to_ticks32(ROTATE_INTERVAL_MS));
    ESP_LOGI(LOG_TAG, "rotation armed, first epoch in %d ms", ROTATE_INTERVAL_MS);
#else
    // Rotation disabled (CONFIG_ESPTAG_ROTATE_ENABLE=n): never arm the timer, so
    // the tag keeps advertising under its boot-epoch identity indefinitely.
    ESP_LOGW(LOG_TAG, "rotation disabled, advertising under a static identity");
#endif // CONFIG_ESPTAG_ROTATE_ENABLE
}

static void on_rotate(struct ble_npl_event *ev) {
    ESP_LOGI(LOG_TAG, "epoch elapsed, rotating from counter=%lu",
             (unsigned long)s_tag->counter);

    if (tag_rotate(s_tag) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "tag rotate failed");
        // Fall through: re-advertise under the unchanged identity rather than
        // going dark, and try again next epoch.
    }
#ifdef CONFIG_ESPTAG_PERSIST_COUNTER
    else if (nvs_store_save_counter(s_tag->counter) != STATUS_OK) {
        // Persist failure is non-fatal: keep advertising under the new identity
        // and retry the write next epoch. The worst case is replaying from an
        // earlier counter after a reboot, not going dark.
        ESP_LOGW(LOG_TAG, "counter persist failed");
    }
#endif // CONFIG_ESPTAG_PERSIST_COUNTER

    // sm_ll_set_key stops and restarts advertising under the new key.
    if (sm_ll_set_key(s_tag->p_curr) != ESP_OK) {
        ESP_LOGE(LOG_TAG, "re-advertising failed");
    }

    ble_npl_callout_reset(&s_rotate_timer,
                          ble_npl_time_ms_to_ticks32(ROTATE_INTERVAL_MS));
}
