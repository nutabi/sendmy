#include "sendmy_carrier.h"

#include "esp_err.h"
#include "esp_log.h"
#include "mbedtls/platform_util.h"
#include "psa/crypto.h"

#include <string.h>

static const char *TAG = "sendmy_carrier";

#define SM_CR_HMAC_LEN 32

_Static_assert(SM_CR_CID_LEN <= SM_CR_HMAC_LEN, "CID must fit in a single HMAC-SHA256 block");

static const uint8_t SM_CR_INFO[SM_CR_INFO_LEN] = {0x73, 0x6D, 0x76, 0x31};

/*
 * ---------------------------------------------------------------------------
 * Component static helper declaration
 * ---------------------------------------------------------------------------
 */

static esp_err_t hkdf_expand_single(const uint8_t prk[SM_CR_UID_LEN], const uint8_t *info, size_t info_len,
                                    uint8_t okm[SM_CR_CID_LEN]);

static esp_err_t compute_cid(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t cid[SM_CR_CID_LEN]);

/*
 * ---------------------------------------------------------------------------
 * Component API implementation
 * ---------------------------------------------------------------------------
 */

esp_err_t sm_cr_build_carrier(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t payload,
                              uint8_t carrier[SM_CR_CARRIER_LEN])
{
    // Compute CID into the first SM_CR_CID_LEN octets of the carrier
    esp_err_t status = compute_cid(uid, mid, carrier);
    if (status != ESP_OK) {
        ESP_LOGE(TAG, "compute_cid failed for mid=%lu: %d", (unsigned long)mid, (int)status);
        return status;
    }

    // Append payload as the final (28th) octet
    carrier[SM_CR_CID_LEN] = payload;

    ESP_LOGD(TAG, "built carrier mid=%lu payload=0x%02x", (unsigned long)mid, payload);
    return ESP_OK;
}

/*
 * ---------------------------------------------------------------------------
 * Component static helper implementation
 * ---------------------------------------------------------------------------
 */

static esp_err_t hkdf_expand_single(const uint8_t prk[SM_CR_UID_LEN], const uint8_t *info, size_t info_len,
                                    uint8_t okm[SM_CR_CID_LEN])
{
    psa_status_t status;

    // Make sure PSA is initialised
    status = psa_crypto_init();
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_crypto_init failed: %d", (int)status);
        return ESP_FAIL;
    }

    // Import PRK as raw HMAC key
    psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
    psa_set_key_type(&attr, PSA_KEY_TYPE_HMAC);
    psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_SIGN_MESSAGE);
    psa_set_key_algorithm(&attr, PSA_ALG_HMAC(PSA_ALG_SHA_256));
    psa_set_key_bits(&attr, 256);

    psa_key_id_t key_id;
    status = psa_import_key(&attr, prk, SM_CR_UID_LEN, &key_id);
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_import_key failed: %d", (int)status);
        return ESP_FAIL;
    }

    // Compute HMAC
    psa_mac_operation_t op = PSA_MAC_OPERATION_INIT;
    status = psa_mac_sign_setup(&op, key_id, PSA_ALG_HMAC(PSA_ALG_SHA_256));
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_mac_sign_setup failed: %d", (int)status);
        goto cleanup_key;
    }

    if (info && info_len > 0) {
        status = psa_mac_update(&op, info, info_len);
        if (status != PSA_SUCCESS) {
            ESP_LOGE(TAG, "psa_mac_update(info) failed: %d", (int)status);
            goto cleanup_op;
        }
    }

    uint8_t ctr = 0x01;
    status = psa_mac_update(&op, &ctr, 1);
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_mac_update(ctr) failed: %d", (int)status);
        goto cleanup_op;
    }

    uint8_t t1[SM_CR_HMAC_LEN];
    size_t mac_len;
    status = psa_mac_sign_finish(&op, t1, sizeof(t1), &mac_len);
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_mac_sign_finish failed: %d", (int)status);
        goto cleanup_key;
    }

    memcpy(okm, t1, SM_CR_CID_LEN);
    mbedtls_platform_zeroize(t1, sizeof(t1));
    psa_destroy_key(key_id);
    return ESP_OK;

cleanup_op:
    psa_mac_abort(&op);
cleanup_key:
    psa_destroy_key(key_id);
    return ESP_FAIL;
}

static esp_err_t compute_cid(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t cid[SM_CR_CID_LEN])
{
    uint8_t info[SM_CR_INFO_LEN + 4];
    memcpy(info, SM_CR_INFO, SM_CR_INFO_LEN);
    info[SM_CR_INFO_LEN + 0] = (mid >> 24) & 0xFF;
    info[SM_CR_INFO_LEN + 1] = (mid >> 16) & 0xFF;
    info[SM_CR_INFO_LEN + 2] = (mid >> 8) & 0xFF;
    info[SM_CR_INFO_LEN + 3] = (mid >> 0) & 0xFF;

    return hkdf_expand_single(uid, info, sizeof(info), cid);
}
