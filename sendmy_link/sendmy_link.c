#include "sendmy_link.h"

#include <string.h>

#include "esp_log.h"
#include "esp_random.h"

#include "nimble/nimble_npl.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_hs_id.h"
#include "host/ble_gap.h"

#define LOG_TAG "sendmy_link"

// Length of the manufacturer payload tail (the bytes after `ff 4c 00`).
#define BLE_ADV_PAYLOAD_LEN 27

/**
 * @brief Apple "offline finding" (Find My-style) manufacturer payload.
 *
 * The 27-byte tail that follows the `ff 4c 00` (manufacturer-specific + Apple
 * company id) header inside the BLE advertising data.
 *
 * Derived from the 28-byte advertising key. The leading 6 bytes of the key
 * become the BLE random address (so the identifier rotates with the key), which
 * is why they are not in this payload. The two MSBs of key byte 0 get displaced
 * by the mandatory 0b11 static-random-address prefix, so they are stashed in
 * key_hi instead, letting a scanner reconstruct the full key.
 */
typedef struct __attribute__((packed)) {
    uint8_t of_type;      // byte 0:  0x12 (offline-finding type)
    uint8_t of_len;       // byte 1:  25   (length of remaining OF data)
    uint8_t status;       // byte 2:  random
    uint8_t key_mid[22];  // bytes 3..24:  key[6..27]
    uint8_t key_hi;       // byte 25: key[0] >> 6
    uint8_t hint;         // byte 26: 0
} ble_adv_payload_t;

_Static_assert(sizeof(ble_adv_payload_t) == BLE_ADV_PAYLOAD_LEN,
               "advertising payload must be 27 bytes");

// Caller's ready callback, invoked once on the host task after the first sync.
static void (*s_on_ready)(void) = NULL;

// Advertising interval (ms), captured at init.
static uint32_t s_adv_interval_ms = 0;

// Current advertising key and whether one has been supplied yet. Written by
// sm_ll_set_key (which may run on any task) and read by the host task when
// it applies the key (on_sync and the apply event), so access is serialised by
// s_key_lock. s_synced and s_ready_called below stay host-task-only.
static uint8_t s_key[SM_LL_KEY_LEN];
static bool s_has_key = false;

// Serialises access to s_key / s_has_key / s_apply_queued between the caller's
// task and the host task. Held only around the small buffer copy below, never
// across a NimBLE GAP call.
static struct ble_npl_mutex s_key_lock;

// Apply request handed to the host task via the default event queue. The host
// task is the sole owner of the GAP state, so the stop/restart runs there.
static struct ble_npl_event s_apply_ev;

// True while an apply event is queued but not yet handled, so a burst of
// set_key calls coalesces into a single apply of the most recent key.
static bool s_apply_queued = false;

// Set once sm_ll_init has brought up the port (and thus the default event
// queue and s_key_lock). Guards set_key against running before init.
static bool s_inited = false;

// Whether the host stack has synced and is ready to advertise.
static bool s_synced = false;

// Whether on_ready has been delivered yet (it fires only on the first sync, so
// a driver can init/arm a rotation timer there exactly once).
static bool s_ready_called = false;

/* Static helper declaration */

// Host stack reset: the controller dropped sync. NimBLE re-syncs and invokes
// on_sync again, so there is nothing to do here but log the reason.
static void on_reset(int reason);

// Host/controller are in sync and the stack is ready. Advertises any buffered
// key and hands control to the caller's on_ready callback.
static void on_sync(void);

// FreeRTOS host task body. nimble_port_run blocks forever (there is no shutdown
// path); the deinit tail runs only if it ever returns.
static void host_task(void *param);

// Build the 27-byte offline-finding payload from the advertising key.
static void build_payload(const uint8_t key[SM_LL_KEY_LEN],
                          ble_adv_payload_t *out);

// Derive the 6-byte BLE random address from key[0..5]. NimBLE expects the
// address little-endian; the Find My address is the key big-endian, so the bytes
// are reversed, then the two MSBs are forced to 0b11 (static random address).
static void build_addr(const uint8_t key[SM_LL_KEY_LEN], uint8_t addr[6]);

// Apply event handler (runs on the host task): snapshot the buffered key and,
// once synced, (re)start advertising under it.
static void apply_ev_cb(struct ble_npl_event *ev);

// Set the random address + advertising data from the given key and (re)start
// advertising. Stops any in-progress advertising first. Runs on the host task.
static esp_err_t adv_apply(const uint8_t key[SM_LL_KEY_LEN]);

/* Header implementation */

esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms) {
    s_on_ready = on_ready;
    s_adv_interval_ms = adv_interval_ms;

    ESP_LOGI(LOG_TAG, "initialising Find My advertising (interval %lu ms)",
             (unsigned long)adv_interval_ms);

    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(LOG_TAG, "nimble port init failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(LOG_TAG, "nimble port initialised, starting host task");

    // The default event queue exists once the port is up. Create the key lock
    // and apply event now so sm_ll_set_key is usable from any task right
    // after init: it buffers the key and defers the GAP work to the host task.
    ble_npl_mutex_init(&s_key_lock);
    ble_npl_event_init(&s_apply_ev, apply_ev_cb, NULL);
    s_inited = true;

    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;

    nimble_port_freertos_init(host_task);
    return ESP_OK;
}

esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN]) {
    if (key == NULL) {
        ESP_LOGE(LOG_TAG, "key is null");
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        ESP_LOGE(LOG_TAG, "set_key called before init");
        return ESP_ERR_INVALID_STATE;
    }

    // Buffer the key and, if no apply is already pending, hand a single request
    // to the host task. Touching GAP from this (arbitrary) caller's task is
    // unsafe — the host task owns the advertising state — so the actual
    // stop/restart happens there, on the next turn of its event loop (i.e. by
    // the next advertising cycle), or from on_sync if not yet synced.
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    memcpy(s_key, key, SM_LL_KEY_LEN);
    s_has_key = true;
    bool post = !s_apply_queued;
    if (post) {
        s_apply_queued = true;
    }
    ble_npl_mutex_release(&s_key_lock);

    // Coalesce: only one apply event is ever in flight. A later set_key that
    // arrives before the event runs just overwrites the buffer above, so the
    // host task always applies the most recent key.
    if (post) {
        ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &s_apply_ev);
    }
    return ESP_OK;
}

/* Static helper implementation */

static void on_reset(int reason) {
    ESP_LOGW(LOG_TAG, "nimble host reset, reason=%d", reason);
}

static void on_sync(void) {
    ESP_LOGI(LOG_TAG, "nimble host synced");
    s_synced = true;

    // Re-advertise whatever key we hold: this also runs after a controller reset
    // re-sync, where the advertising state was lost. On the very first sync no
    // key is buffered yet (the driver supplies it from on_ready below), so this
    // is skipped.
    uint8_t key[SM_LL_KEY_LEN];
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    bool has_key = s_has_key;
    if (has_key) {
        memcpy(key, s_key, SM_LL_KEY_LEN);
    }
    ble_npl_mutex_release(&s_key_lock);

    if (has_key && adv_apply(key) != ESP_OK) {
        ESP_LOGE(LOG_TAG, "advertising start failed");
    }

    // Deliver on_ready exactly once, on the first sync. The driver typically sets
    // the initial key (sm_ll_set_key) and arms its rotation timer here —
    // init-once, so it is safe to set up a timer without re-initing it on every
    // re-sync.
    if (s_on_ready != NULL && !s_ready_called) {
        s_ready_called = true;
        s_on_ready();
    }
}

static void apply_ev_cb(struct ble_npl_event *ev) {
    (void)ev;

    // Snapshot the buffered key and clear the pending flag together, so a
    // set_key that races in after this point queues a fresh apply rather than
    // being lost.
    uint8_t key[SM_LL_KEY_LEN];
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    s_apply_queued = false;
    bool has_key = s_has_key;
    if (has_key) {
        memcpy(key, s_key, SM_LL_KEY_LEN);
    }
    ble_npl_mutex_release(&s_key_lock);

    // Before sync there is no controller to advertise on; on_sync applies the
    // buffered key once the stack is ready.
    if (has_key && s_synced && adv_apply(key) != ESP_OK) {
        ESP_LOGE(LOG_TAG, "advertising update failed");
    }
}

static void host_task(void *param) {
    ESP_LOGI(LOG_TAG, "nimble host task started");
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void build_payload(const uint8_t key[SM_LL_KEY_LEN],
                          ble_adv_payload_t *out) {
    out->of_type = 0x12;
    out->of_len = 25;
    out->status = (uint8_t)esp_random();
    memcpy(out->key_mid, &key[6], sizeof(out->key_mid));
    out->key_hi = key[0] >> 6;
    out->hint = 0;
}

static void build_addr(const uint8_t key[SM_LL_KEY_LEN], uint8_t addr[6]) {
    for (int i = 0; i < 6; i++) {
        addr[i] = key[5 - i];
    }
    addr[5] |= 0xC0;  // top two bits = 0b11: static random address
}

static esp_err_t adv_apply(const uint8_t key[SM_LL_KEY_LEN]) {
    // The random address and advertising data can only change while stopped.
    // BLE_HS_EALREADY just means advertising was not running, not an error.
    int rc = ble_gap_adv_stop();
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGW(LOG_TAG, "adv stop returned %d", rc);
    }

    uint8_t addr[6];
    build_addr(key, addr);
    rc = ble_hs_id_set_rnd(addr);
    if (rc != 0) {
        ESP_LOGE(LOG_TAG, "set random address failed: %d", rc);
        return ESP_FAIL;
    }

    // Full advertising data: 0x1e length, 0xff manufacturer-specific, 0x4c 0x00
    // Apple company id, then the 27-byte offline-finding payload = 31 bytes.
    uint8_t data[4 + BLE_ADV_PAYLOAD_LEN] = {0x1e, 0xff, 0x4c, 0x00};
    ble_adv_payload_t pl;
    build_payload(key, &pl);
    memcpy(&data[4], &pl, sizeof(pl));

    rc = ble_gap_adv_set_data(data, sizeof(data));
    if (rc != 0) {
        ESP_LOGE(LOG_TAG, "set adv data failed: %d", rc);
        return ESP_FAIL;
    }

    // Advertise once every s_adv_interval_ms. A single legacy advertising event
    // already transmits the PDU once on each enabled advertising channel
    // (37/38/39), so pinning itvl_min == itvl_max yields exactly one sweep of all
    // three channels per window (plus the mandatory 0-10 ms advDelay the
    // controller adds). BLE_GAP_ADV_ITVL_MS converts ms to the 0.625 ms HCI units.
    struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_NON,
        .disc_mode = BLE_GAP_DISC_MODE_NON,
        .itvl_min = BLE_GAP_ADV_ITVL_MS(s_adv_interval_ms),
        .itvl_max = BLE_GAP_ADV_ITVL_MS(s_adv_interval_ms),
    };
    rc = ble_gap_adv_start(BLE_OWN_ADDR_RANDOM, NULL, BLE_HS_FOREVER, &params,
                           NULL, NULL);
    if (rc != 0) {
        ESP_LOGE(LOG_TAG, "adv start failed: %d", rc);
        return ESP_FAIL;
    }

    // Log the address MSB-first (the order a scanner displays it) so it can be
    // matched against the scan.
    ESP_LOGI(LOG_TAG, "advertising as %02X:%02X:%02X:%02X:%02X:%02X",
             addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]);
    ESP_LOG_BUFFER_HEX_LEVEL(LOG_TAG, data, sizeof(data), ESP_LOG_DEBUG);
    return ESP_OK;
}
