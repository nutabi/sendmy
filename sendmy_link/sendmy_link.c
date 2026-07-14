#include "sendmy_link.h"

#include "esp_bt.h"
#include "esp_log.h"
#include "host/ble_gap.h"
#include "host/ble_hs.h"
#include "host/ble_hs_id.h"
#include "nimble/nimble_npl.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

#include <string.h>

static const char *TAG = "sendmy_link";

#define BLE_ADV_PAYLOAD_LEN 27

typedef struct __attribute__((packed)) {
    uint8_t of_type;
    uint8_t of_len;
    uint8_t status;
    uint8_t key_mid[22];
    uint8_t key_hi;
    uint8_t hint;
} ble_adv_payload_t;

/*
 * ----------------------------------------------------------------------------
 * Component states - Callbacks and BLE parameters
 * ----------------------------------------------------------------------------
 */

static void (*s_on_ready)(void) = NULL;
static uint32_t s_adv_interval_ms = 0;
static uint8_t s_key[SM_LL_KEY_LEN];

// Desired advertising TX power. s_tx_power_pending is set when a new value has
// been requested but not yet pushed to the controller; it is applied inside
// adv_apply() -- the single chokepoint for every advertisement -- so the power
// is guaranteed in effect before the first (and every) packet goes on air.
static int8_t s_tx_power_dbm = 0;
static bool s_tx_power_pending = false;

/*
 * ----------------------------------------------------------------------------
 * Component states - Mutex for multi-threading safety
 * ----------------------------------------------------------------------------
 */

static bool s_has_key = false;
static struct ble_npl_mutex s_key_lock;
static struct ble_npl_event s_apply_ev;
static bool s_apply_queued = false;
static bool s_inited = false;
static bool s_synced = false;
static bool s_ready_called = false;

/*
 * ----------------------------------------------------------------------------
 * Component static helper declaration
 * ----------------------------------------------------------------------------
 */

static void on_reset(int reason);
static void on_sync(void);
static void host_task(void *param);
static void build_payload(const uint8_t key[SM_LL_KEY_LEN], ble_adv_payload_t *out);
static void build_addr(const uint8_t key[SM_LL_KEY_LEN], uint8_t addr[6]);
static void apply_ev_cb(struct ble_npl_event *ev);
static esp_err_t adv_apply(const uint8_t key[SM_LL_KEY_LEN]);
static esp_err_t apply_tx_power(void);

/*
 * ----------------------------------------------------------------------------
 * Component API implementation
 * ----------------------------------------------------------------------------
 */

esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms)
{
    // BLE advertising uses 0.625 ms units; valid HCI range is 0x20..0x4000,
    // i.e. 20..10240 ms. Out-of-range values convert to an out-of-spec interval
    // and advertising would silently fail to start.
    if (adv_interval_ms < 20 || adv_interval_ms > 10240) {
        ESP_LOGE(TAG, "adv_interval_ms %lu out of range [20, 10240]", (unsigned long)adv_interval_ms);
        return ESP_ERR_INVALID_ARG;
    }

    // A bunch of initialisation stuff, nothing special here

    s_on_ready = on_ready;
    s_adv_interval_ms = adv_interval_ms;

    ESP_LOGI(TAG, "initialising OF advertising (interval %lu ms)", (unsigned long)adv_interval_ms);

    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble port init failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "nimble port initialised, starting host task");

    ble_npl_mutex_init(&s_key_lock);
    ble_npl_event_init(&s_apply_ev, apply_ev_cb, NULL);
    s_inited = true;

    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;

    nimble_port_freertos_init(host_task);
    return ESP_OK;
}

esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN])
{
    // Sanity checks
    if (key == NULL) {
        ESP_LOGE(TAG, "key is null");
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        ESP_LOGE(TAG, "set_key called before init");
        return ESP_ERR_INVALID_STATE;
    }

    // Put key in buffer
    // 1. Get the lock (wait as long as needed)
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    // 2. Copy the key
    memcpy(s_key, key, SM_LL_KEY_LEN);
    s_has_key = true;
    bool post = !s_apply_queued;
    if (post) {
        s_apply_queued = true;
    }
    // 3. Release the lock
    ble_npl_mutex_release(&s_key_lock);

    // Tell NimBLE host that there is a new key
    // Host will resync and apply the key as needed.
    if (post) {
        ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &s_apply_ev);
    }
    return ESP_OK;
}

esp_err_t sm_ll_set_tx_power(int8_t dbm)
{
    if (!s_inited) {
        // The controller is brought up by sm_ll_init(); requesting a power
        // before that is a usage error.
        ESP_LOGE(TAG, "set_tx_power called before init");
        return ESP_ERR_INVALID_STATE;
    }

    // Only record the request here. adv_apply() pushes it to the controller on
    // the host task right before advertising starts, so the power is in effect
    // before the first packet regardless of how init and this call interleave.
    s_tx_power_dbm = dbm;
    s_tx_power_pending = true;
    return ESP_OK;
}

/*
 * ----------------------------------------------------------------------------
 * Component static helper implementation
 * ----------------------------------------------------------------------------
 */

static void on_reset(int reason)
{
    // Only called on (re)boots and catastrophic errors
    ESP_LOGW(TAG, "nimble host reset, reason=%d", reason);
    s_synced = false;
}

static void on_sync(void)
{
    // Called after a while or host detects a new key
    ESP_LOGI(TAG, "nimble host synced");
    s_synced = true;

    // Get key from buffer
    // Like writing, this involves a mutex lock
    uint8_t key[SM_LL_KEY_LEN];
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    bool has_key = s_has_key;
    if (has_key) {
        memcpy(key, s_key, SM_LL_KEY_LEN);
    }
    ble_npl_mutex_release(&s_key_lock);

    // Update advertising parameters and restart advertising
    if (has_key && adv_apply(key) != ESP_OK) {
        ESP_LOGE(TAG, "advertising start failed");
    }

    if (s_on_ready != NULL && !s_ready_called) {
        s_ready_called = true;
        s_on_ready();
    }
}

static void apply_ev_cb(struct ble_npl_event *ev)
{
    (void)ev;

    // Get key from buffer
    uint8_t key[SM_LL_KEY_LEN];
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    s_apply_queued = false;
    bool has_key = s_has_key;
    if (has_key) {
        memcpy(key, s_key, SM_LL_KEY_LEN);
    }
    ble_npl_mutex_release(&s_key_lock);

    // Restart advertising
    if (has_key && s_synced && adv_apply(key) != ESP_OK) {
        ESP_LOGE(TAG, "advertising update failed");
    }
}

static void host_task(void *param)
{
    // Standard startup stuff
    ESP_LOGI(TAG, "nimble host task started");
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void build_payload(const uint8_t key[SM_LL_KEY_LEN], ble_adv_payload_t *out)
{
    // Build the OF payload
    out->of_type = 0x12;
    out->of_len = 25;
    out->status = 0x00;
    memcpy(out->key_mid, &key[6], sizeof(out->key_mid));
    out->key_hi = key[0] >> 6;
    out->hint = 0;
}

static void build_addr(const uint8_t key[SM_LL_KEY_LEN], uint8_t addr[6])
{
    // Build BLE address
    for (int i = 0; i < 6; i++) {
        addr[i] = key[5 - i];
    }

    // Per BLE standard, random static address has top 2 bits in MSB set to 1.
    // Which is also why the protocol stores the MSB again in the payload.
    addr[5] |= 0xC0;
}

static esp_err_t adv_apply(const uint8_t key[SM_LL_KEY_LEN])
{
    // Stop advertising
    int rc = ble_gap_adv_stop();
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGW(TAG, "adv stop returned %d", rc);
    }

    // Set address
    uint8_t addr[6];
    build_addr(key, addr);
    rc = ble_hs_id_set_rnd(addr);
    if (rc != 0) {
        ESP_LOGE(TAG, "set random address failed: %d", rc);
        return ESP_FAIL;
    }

    // Set payload
    // Other than the 27-byte payload, it also sets 4 bytes for AD type, AD
    // length, and manufacturer ID
    uint8_t data[4 + BLE_ADV_PAYLOAD_LEN] = {0x1e, 0xff, 0x4c, 0x00};
    ble_adv_payload_t pl;
    build_payload(key, &pl);
    memcpy(&data[4], &pl, sizeof(pl));

    rc = ble_gap_adv_set_data(data, sizeof(data));
    if (rc != 0) {
        ESP_LOGE(TAG, "set adv data failed: %d", rc);
        return ESP_FAIL;
    }

    // Push any pending TX power to the controller before advertising starts, so
    // the very first packet radiates at the configured power.
    if (s_tx_power_pending && apply_tx_power() != ESP_OK) {
        ESP_LOGW(TAG, "tx power apply failed; advertising at controller default");
    }

    // Non-connectable, non-directed advertising
    // Also, set the interval such that only one advertising event happens
    // (once per channel)
    struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_NON,
        .disc_mode = BLE_GAP_DISC_MODE_NON,
        .itvl_min = BLE_GAP_ADV_ITVL_MS(s_adv_interval_ms),
        .itvl_max = BLE_GAP_ADV_ITVL_MS(s_adv_interval_ms),
    };

    // Restart advertising
    rc = ble_gap_adv_start(BLE_OWN_ADDR_RANDOM, NULL, BLE_HS_FOREVER, &params, NULL, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv start failed: %d", rc);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "advertising as %02X:%02X:%02X:%02X:%02X:%02X", addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]);
    ESP_LOG_BUFFER_HEX_LEVEL(TAG, data, sizeof(data), ESP_LOG_DEBUG);
    return ESP_OK;
}

static esp_err_t apply_tx_power(void)
{
    // The controller exposes power as esp_power_level_t: 3 dBm steps from
    // ESP_PWR_LVL_N24 (-24 dBm, index 0) to the top of the range (index 15).
    // Map the requested dBm onto the nearest index and clamp to the range.
    int8_t dbm = s_tx_power_dbm;
    if (dbm < -24) {
        dbm = -24;
    }
    int idx = (dbm + 24 + 1) / 3;  // +1 biases .5 upward before truncation
    if (idx < ESP_PWR_LVL_N24) {
        idx = ESP_PWR_LVL_N24;
    }
    if (idx > ESP_PWR_LVL_P20) {
        idx = ESP_PWR_LVL_P20;
    }
    int applied_dbm = -24 + 3 * idx;

    // ADV covers advertising/scan-response; DEFAULT is the fallback for any
    // path that has not set its own level. Set both so the advertising power is
    // unambiguous regardless of controller defaults.
    esp_err_t rc = esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, (esp_power_level_t)idx);
    if (rc == ESP_OK) {
        rc = esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, (esp_power_level_t)idx);
    }
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "set tx power failed: %s", esp_err_to_name(rc));
        return rc;
    }

    s_tx_power_pending = false;
    ESP_LOGI(TAG, "tx power set to %d dBm (requested %d)", applied_dbm, dbm);
    return ESP_OK;
}
