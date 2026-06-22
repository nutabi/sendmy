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
static void build_payload(const uint8_t key[SM_LL_KEY_LEN],
                          ble_adv_payload_t *out);
static void build_addr(const uint8_t key[SM_LL_KEY_LEN], uint8_t addr[6]);
static void apply_ev_cb(struct ble_npl_event *ev);
static esp_err_t adv_apply(const uint8_t key[SM_LL_KEY_LEN]);

/*
 * ----------------------------------------------------------------------------
 * Component API implementation
 * ----------------------------------------------------------------------------
 */

esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms) {
    s_on_ready = on_ready;
    s_adv_interval_ms = adv_interval_ms;

    ESP_LOGI(TAG, "initialising Find My advertising (interval %lu ms)",
             (unsigned long)adv_interval_ms);

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

esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN]) {
    if (key == NULL) {
        ESP_LOGE(TAG, "key is null");
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        ESP_LOGE(TAG, "set_key called before init");
        return ESP_ERR_INVALID_STATE;
    }

    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    memcpy(s_key, key, SM_LL_KEY_LEN);
    s_has_key = true;
    bool post = !s_apply_queued;
    if (post) {
        s_apply_queued = true;
    }
    ble_npl_mutex_release(&s_key_lock);

    if (post) {
        ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &s_apply_ev);
    }
    return ESP_OK;
}

/*
 * ----------------------------------------------------------------------------
 * Component static helper implementation
 * ----------------------------------------------------------------------------
 */

static void on_reset(int reason) {
    ESP_LOGW(TAG, "nimble host reset, reason=%d", reason);
}

static void on_sync(void) {
    ESP_LOGI(TAG, "nimble host synced");
    s_synced = true;

    uint8_t key[SM_LL_KEY_LEN];
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    bool has_key = s_has_key;
    if (has_key) {
        memcpy(key, s_key, SM_LL_KEY_LEN);
    }
    ble_npl_mutex_release(&s_key_lock);

    if (has_key && adv_apply(key) != ESP_OK) {
        ESP_LOGE(TAG, "advertising start failed");
    }

    if (s_on_ready != NULL && !s_ready_called) {
        s_ready_called = true;
        s_on_ready();
    }
}

static void apply_ev_cb(struct ble_npl_event *ev) {
    (void)ev;

    uint8_t key[SM_LL_KEY_LEN];
    ble_npl_mutex_pend(&s_key_lock, BLE_NPL_TIME_FOREVER);
    s_apply_queued = false;
    bool has_key = s_has_key;
    if (has_key) {
        memcpy(key, s_key, SM_LL_KEY_LEN);
    }
    ble_npl_mutex_release(&s_key_lock);

    if (has_key && s_synced && adv_apply(key) != ESP_OK) {
        ESP_LOGE(TAG, "advertising update failed");
    }
}

static void host_task(void *param) {
    ESP_LOGI(TAG, "nimble host task started");
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
    addr[5] |= 0xC0;
}

static esp_err_t adv_apply(const uint8_t key[SM_LL_KEY_LEN]) {
    int rc = ble_gap_adv_stop();
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGW(TAG, "adv stop returned %d", rc);
    }

    uint8_t addr[6];
    build_addr(key, addr);
    rc = ble_hs_id_set_rnd(addr);
    if (rc != 0) {
        ESP_LOGE(TAG, "set random address failed: %d", rc);
        return ESP_FAIL;
    }

    uint8_t data[4 + BLE_ADV_PAYLOAD_LEN] = {0x1e, 0xff, 0x4c, 0x00};
    ble_adv_payload_t pl;
    build_payload(key, &pl);
    memcpy(&data[4], &pl, sizeof(pl));

    rc = ble_gap_adv_set_data(data, sizeof(data));
    if (rc != 0) {
        ESP_LOGE(TAG, "set adv data failed: %d", rc);
        return ESP_FAIL;
    }

    struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_NON,
        .disc_mode = BLE_GAP_DISC_MODE_NON,
        .itvl_min = BLE_GAP_ADV_ITVL_MS(s_adv_interval_ms),
        .itvl_max = BLE_GAP_ADV_ITVL_MS(s_adv_interval_ms),
    };
    rc = ble_gap_adv_start(BLE_OWN_ADDR_RANDOM, NULL, BLE_HS_FOREVER, &params,
                           NULL, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv start failed: %d", rc);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "advertising as %02X:%02X:%02X:%02X:%02X:%02X",
             addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]);
    ESP_LOG_BUFFER_HEX_LEVEL(TAG, data, sizeof(data), ESP_LOG_DEBUG);
    return ESP_OK;
}
