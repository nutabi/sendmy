#include "tag.h"

#include "crypto.h"

#include <string.h>

#include "esp_log.h"

#include "mbedtls/platform_util.h"

#define LOG_TAG "tag"

/* Header implementation */

status_t tag_init(tag_t *tag, uint32_t counter) {
    if (tag == NULL) {
        ESP_LOGE(LOG_TAG, "tag is null");
        return STATUS_ERR;
    }

    // Resume at the persisted epoch: fast-forward the ratchet from sk_0 by
    // `counter` steps so identifiers continue rather than replay across reboots.
    // counter == 0 leaves sk_curr == sk_0 (the first-boot / fresh-seed case).
    tag->counter = counter;
    if (crypto_advance_sk(tag->sk_0, counter, tag->sk_curr) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "sk advance failed");
        return STATUS_ERR;
    }

    // Derive the current advertising key: p_i = derive_p(d_0, sk_i).
    if (crypto_derive_p(tag->d_0, tag->sk_curr, tag->p_curr) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "p derivation failed");
        return STATUS_ERR;
    }
    return STATUS_OK;
}

status_t tag_rotate(tag_t *tag) {
    if (tag == NULL) {
        ESP_LOGE(LOG_TAG, "tag is null");
        return STATUS_ERR;
    }

    // Update SK
    uint8_t sk_next[SK_LEN];
    if (crypto_update_sk(tag->sk_curr, sk_next) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "sk rotation failed");
#ifdef CONFIG_ESPTAG_ZEROIZE
        mbedtls_platform_zeroize(sk_next, sizeof(sk_next));
#endif // CONFIG_ESPTAG_ZEROIZE
        return STATUS_ERR;
    }

    // Derive P
    if (crypto_derive_p(tag->d_0, sk_next, tag->p_curr) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "p derivation failed");
#ifdef CONFIG_ESPTAG_ZEROIZE
        mbedtls_platform_zeroize(sk_next, sizeof(sk_next));
#endif // CONFIG_ESPTAG_ZEROIZE
        return STATUS_ERR;
    }

    // Update tag
    memcpy(tag->sk_curr, sk_next, SK_LEN);
    tag->counter++;
#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(sk_next, sizeof(sk_next));
#endif // CONFIG_ESPTAG_ZEROIZE
    return STATUS_OK;
}

status_t tag_destroy(tag_t *tag) {
    if (tag == NULL) {
        ESP_LOGE(LOG_TAG, "tag is null");
        return STATUS_ERR;
    }

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(tag, sizeof(*tag));
#endif // CONFIG_ESPTAG_ZEROIZE
    return STATUS_OK;
}
