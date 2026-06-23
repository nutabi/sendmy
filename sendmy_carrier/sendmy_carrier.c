#include "sendmy_carrier.h"

#include "p224.h"

#include "esp_err.h"
#include "esp_log.h"
#include "mbedtls/platform_util.h"
#include "psa/crypto.h"

#include <string.h>

static const char *TAG = "sendmy_carrier";

#define SM_CR_HMAC_LEN 32

_Static_assert(SM_CR_CARRIER_LEN <= SM_CR_HMAC_LEN,
               "carrier must fit in a single HMAC-SHA256 block");

_Static_assert(SM_CR_CARRIER_LEN == P224_LEN,
               "carrier is the 28-byte x-coordinate of a P-224 point");

static const uint8_t SM_CR_INFO[SM_CR_INFO_LEN] = {0x73, 0x6D, 0x76, 0x31};

/*
 * ---------------------------------------------------------------------------
 * Component static helper declaration
 * ---------------------------------------------------------------------------
 */

static esp_err_t hkdf_expand_single(const uint8_t prk[SM_CR_UID_LEN], const uint8_t *info,
                                    size_t info_len, uint8_t okm[SM_CR_CARRIER_LEN]);

static esp_err_t compute_carrier(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t payload,
                                 uint8_t carrier[SM_CR_CARRIER_LEN]);

/*
 * ---------------------------------------------------------------------------
 * Component API implementation
 * ---------------------------------------------------------------------------
 */

esp_err_t sm_cr_build_carrier(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t payload,
                              uint8_t carrier[SM_CR_CARRIER_LEN])
{
    // Derive the whole 28-byte carrier from (uid, mid, payload). The payload is
    // folded into the HKDF info, so it is bound to the key rather than appended
    // verbatim and never appears in the advertisement in the clear.
    esp_err_t status = compute_carrier(uid, mid, payload, carrier);
    if (status != ESP_OK) {
        ESP_LOGE(TAG, "compute_carrier failed for mid=%lu: %d", (unsigned long)mid, (int)status);
        return status;
    }

    ESP_LOGD(TAG, "built carrier mid=%lu payload=0x%02x", (unsigned long)mid, payload);
    return ESP_OK;
}

/*
 * ---------------------------------------------------------------------------
 * Component static helper implementation
 * ---------------------------------------------------------------------------
 */

static esp_err_t hkdf_expand_single(const uint8_t prk[SM_CR_UID_LEN], const uint8_t *info,
                                    size_t info_len, uint8_t okm[SM_CR_CARRIER_LEN])
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

    memcpy(okm, t1, SM_CR_CARRIER_LEN);
    mbedtls_platform_zeroize(t1, sizeof(t1));
    psa_destroy_key(key_id);
    return ESP_OK;

cleanup_op:
    psa_mac_abort(&op);
cleanup_key:
    psa_destroy_key(key_id);
    return ESP_FAIL;
}

static esp_err_t compute_carrier(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t payload,
                                 uint8_t carrier[SM_CR_CARRIER_LEN])
{
    // info = "smv1" || mid_be32 || payload || attempt
    //
    // The attempt byte is always present (even on the first, almost-always-taken
    // iteration). Each iteration derives a fresh 28-byte block, interprets it as
    // a big-endian scalar d, and keeps it only if d lands in the valid P-224
    // range [1, n-1]. P(reject) ~ 2^-112, so in practice the loop never spins.
    uint8_t info[SM_CR_INFO_LEN + 4 + 2];
    memcpy(info, SM_CR_INFO, SM_CR_INFO_LEN);
    info[SM_CR_INFO_LEN + 0] = (mid >> 24) & 0xFF;
    info[SM_CR_INFO_LEN + 1] = (mid >> 16) & 0xFF;
    info[SM_CR_INFO_LEN + 2] = (mid >> 8) & 0xFF;
    info[SM_CR_INFO_LEN + 3] = (mid >> 0) & 0xFF;
    info[SM_CR_INFO_LEN + 4] = payload;

    uint8_t block[SM_CR_CARRIER_LEN];
    esp_err_t status = ESP_FAIL;

    // attempt is a single byte, so it can take at most 256 distinct values.
    for (unsigned attempt = 0; attempt <= 0xFF; attempt++) {
        info[SM_CR_INFO_LEN + 5] = (uint8_t)attempt;

        status = hkdf_expand_single(uid, info, sizeof(info), block);
        if (status != ESP_OK) {
            // hkdf_expand_single already logged the underlying failure.
            goto cleanup;
        }

        if (p224_scalar_in_range(block)) {
            // block is a valid scalar d; the carrier is X(d*G).
            status = p224_base_mult_x(block, carrier);
            goto cleanup;
        }

        ESP_LOGD(TAG, "scalar out of range, retrying (attempt=%u)", attempt);
    }

    // Exhausting all 256 attempts is astronomically improbable; treat as failure.
    ESP_LOGE(TAG, "no valid P-224 scalar after 256 attempts for mid=%lu", (unsigned long)mid);
    status = ESP_FAIL;

cleanup:
    mbedtls_platform_zeroize(block, sizeof(block));
    mbedtls_platform_zeroize(info, sizeof(info));
    return status;
}
