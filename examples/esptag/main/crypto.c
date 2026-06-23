#include "crypto.h"

#include "esp_log.h"
#include "esp_random.h"

#include <assert.h>
#include <stddef.h>
#include <string.h>

/* bignum.h (the ESP-IDF port shim) must precede psa/crypto.h: it defines
 * MBEDTLS_DECLARE_PRIVATE_IDENTIFIERS before pulling in mbedtls/private/bignum.h,
 * which is what exposes the legacy mbedtls_mpi_* API under mbedTLS 4.x. If
 * psa/crypto.h includes the private header first (without that macro), the
 * include guard is set and the MPI declarations are silently skipped. */
#include "mbedtls/bignum.h"
#include "mbedtls/platform_util.h"
#include "psa/crypto.h"
#include "uECC.h"

#define LOG_TAG "crypto"

/* Extra P-224 constants */
#define P224_PUB_KEY_LEN 56
#define P224_COMPRESSED_LEN 29

/* KDF constants */
#define LABEL_UPDATE "update"
#define LABEL_DIVERSIFY "diversify"
#define Z_LEN SK_LEN
#define COUNTER_LEN 4
#define MAX_INFO_LEN 9
#define MAX_IN_LEN (SK_LEN + COUNTER_LEN + MAX_INFO_LEN)
#define MAX_BUF_LEN (MAX_IN_LEN * 2)

/* Advertising key rolling constants */
#define UV_LEN 36
#define DIVERSIFY_LEN (UV_LEN * 2)

/* P-224 (secp224r1) group order n, big-endian */
const uint8_t P224_N[N_LEN] = {
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0x16, 0xA2, 0xE0, 0xB8, 0xF0, 0x3E, 0x13, 0xDD, 0x29, 0x45, 0x5C, 0x5C, 0x2A, 0x3D,
};

/* Static variables */
static uECC_Curve curve;

/* Static helper declaration */

static int uecc_rng(uint8_t dst[], unsigned len);

static status_t d_to_p(const uint8_t d[D_LEN], uint8_t p[P_LEN]);

static status_t hash(const uint8_t in[], size_t in_len, uint8_t out[HASH_LEN]);

static status_t kdf(const uint8_t z[], size_t z_len, const char info[], size_t info_len, uint8_t out[], size_t out_len);

static status_t compute_d(const uint8_t d_0[D_LEN], const uint8_t u[UV_LEN], const uint8_t v[UV_LEN],
                          uint8_t d_i[D_LEN]);

/* Header implementation */

status_t crypto_init(void)
{
    if (psa_crypto_init() != PSA_SUCCESS) {
        ESP_LOGE(LOG_TAG, "crypto initialization failed");
        return STATUS_ERR;
    }

    curve = uECC_secp224r1();
    uECC_set_rng(uecc_rng);
    return STATUS_OK;
}

status_t crypto_advance_sk(const uint8_t sk_0[SK_LEN], uint32_t counter, uint8_t sk_i[SK_LEN])
{
    // Set up buffer
    uint8_t buf[2][SK_LEN];
    uint8_t *sk_curr = buf[0];
    uint8_t *sk_next = buf[1];

    // Copy SK_0
    memcpy(sk_curr, sk_0, SK_LEN);

    for (uint32_t i = 0; i < counter; i++) {
        if (crypto_update_sk(sk_curr, sk_next) != STATUS_OK) {
            ESP_LOGE(LOG_TAG, "sk update failed");

#ifdef CONFIG_ESPTAG_ZEROIZE
            mbedtls_platform_zeroize(buf, sizeof(buf));
#endif  // CONFIG_ESPTAG_ZEROIZE

            return STATUS_ERR;
        }

        // Swap sk_curr and sk_next
        uint8_t *tmp = sk_curr;
        sk_curr = sk_next;
        sk_next = tmp;
    }

    // Copy SK_i
    memcpy(sk_i, sk_curr, SK_LEN);

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(buf, sizeof(buf));
#endif  // CONFIG_ESPTAG_ZEROIZE

    return STATUS_OK;
}

status_t crypto_update_sk(const uint8_t sk_prev[SK_LEN], uint8_t sk_next[SK_LEN])
{
    // Compute sk_next = KDF(sk_prev, "update", 32)
    if (kdf(sk_prev, SK_LEN, LABEL_UPDATE, sizeof(LABEL_UPDATE) - 1,  // Ignore null
            sk_next, SK_LEN) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "kdf failed");
        return STATUS_ERR;
    }
    return STATUS_OK;
}

status_t crypto_derive_p(const uint8_t d_0[D_LEN], const uint8_t sk_i[SK_LEN], uint8_t p_i[P_LEN])
{
    // Compute (u_i || v_i) = KDF(sk_i, "diversify", 72)
    uint8_t uv[DIVERSIFY_LEN];
    if (kdf(sk_i, SK_LEN, LABEL_DIVERSIFY, sizeof(LABEL_DIVERSIFY) - 1,  // Ignore null
            uv, sizeof(uv)) != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "uv calculation failed");
        // On a late-block KDF failure uv holds partial output; (u,v) reveals d_0
        // given any emitted p_i, so scrub it here too (every-path invariant).
#ifdef CONFIG_ESPTAG_ZEROIZE
        mbedtls_platform_zeroize(uv, sizeof(uv));
#endif  // CONFIG_ESPTAG_ZEROIZE
        return STATUS_ERR;
    }

    // Compute d_i = d_0 * u_i + v_i mod n
    uint8_t d_i[D_LEN];
    status_t rc = compute_d(d_0, uv, uv + UV_LEN, d_i);

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(uv, sizeof(uv));
#endif  // CONFIG_ESPTAG_ZEROIZE

    if (rc != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "d_i computation failed");
        return STATUS_ERR;
    }

    // Compute p_i = d_i * G
    rc = d_to_p(d_i, p_i);

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(d_i, sizeof(d_i));
#endif  // CONFIG_ESPTAG_ZEROIZE

    if (rc != STATUS_OK) {
        ESP_LOGE(LOG_TAG, "p_i computation failed");
        return STATUS_ERR;
    }
    return STATUS_OK;
}

/* Static helper implementation */

static int uecc_rng(uint8_t dst[], unsigned len)
{
    // NOTE: this follows micro-ecc's RNG contract (1 = success, 0 = failure),
    // which is the INVERSE of this project's house convention (0 = success).
    // esp_fill_random cannot fail, so we unconditionally report success (1).
    esp_fill_random(dst, len);
    return 1;
}

static status_t d_to_p(const uint8_t d[D_LEN], uint8_t p[P_LEN])
{
    // Compute public key
    uint8_t full_key[P224_PUB_KEY_LEN];
    if (uECC_compute_public_key(d, full_key, curve) != 1) {
        ESP_LOGE(LOG_TAG, "public key computation failed");
        return STATUS_ERR;
    }

    // Compute compressed public key
    uint8_t compressed_key[P224_COMPRESSED_LEN];
    uECC_compress(full_key, compressed_key, curve);

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(full_key, sizeof(full_key));
#endif  // CONFIG_ESPTAG_ZEROIZE

    // Copy to p, except the header byte
    memcpy(p, compressed_key + 1, P_LEN);

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(compressed_key, sizeof(compressed_key));
#endif  // CONFIG_ESPTAG_ZEROIZE

    return STATUS_OK;
}

static status_t hash(const uint8_t in[], size_t in_len, uint8_t out[HASH_LEN])
{
    size_t actual_len;
    const psa_status_t status = psa_hash_compute(PSA_ALG_SHA_256, in, in_len, out, HASH_LEN, &actual_len);

    if (status != PSA_SUCCESS) {
        ESP_LOGE(LOG_TAG, "SHA256 hash computation failed (code: %d)", (int)status);
        return STATUS_ERR;
    }

    if (actual_len != HASH_LEN) {
        ESP_LOGE(LOG_TAG, "SHA256 hash length is invalid (expect %d, got %zu)", HASH_LEN, actual_len);
        return STATUS_ERR;
    }

    return STATUS_OK;
}

static status_t kdf(const uint8_t z[], size_t z_len, const char info[], size_t info_len, uint8_t out[], size_t out_len)
{
    // Input buffer layout: z || counter || info
    // 1. Validate (programmer-error preconditions). These are all fixed by the
    //    two call sites (crypto_update_sk / compute_d pass SK_LEN keys and the
    //    "update"/"diversify" literals), so they cannot vary at runtime: assert
    //    rather than warn-and-proceed.
    assert(z_len == SK_LEN);
    assert(info_len > 0 && info_len <= MAX_INFO_LEN);
    // 2. Calculate actual input length
    const size_t in_len = z_len + COUNTER_LEN + info_len;
    // 3. Validate (hard, to avoid buffer overlow)
    if (in_len > MAX_BUF_LEN) {
        ESP_LOGE(LOG_TAG, "input length exceeds buffer limit of %d (got %zu)", MAX_BUF_LEN, in_len);
        return STATUS_ERR;
    }

    // Prepare input buffer
    // - Counter is encoded as 4-byte big-endian integer (filled in loop)
    uint8_t in_buf[MAX_BUF_LEN];
    memcpy(in_buf, z, z_len);
    memcpy(in_buf + z_len + COUNTER_LEN, info, info_len);

    // Prepare hash block
    const size_t num_blocks = (out_len + HASH_LEN - 1) / HASH_LEN;
    uint8_t block[HASH_LEN];

    // Loop
    status_t rc = STATUS_OK;
    for (uint32_t counter = 1; counter <= num_blocks; counter++) {
        // Encode counter (big-endian = MSB at lowest address)
        in_buf[z_len + 0] = (uint8_t)(counter >> 24);
        in_buf[z_len + 1] = (uint8_t)(counter >> 16);
        in_buf[z_len + 2] = (uint8_t)(counter >> 8);
        in_buf[z_len + 3] = (uint8_t)(counter >> 0);

        // Compute hash
        if (hash(in_buf, in_len, block) != STATUS_OK) {
            ESP_LOGE(LOG_TAG, "hash computation failed");
            rc = STATUS_ERR;
            break;
        }

        // Copy to output buffer
        // If last block, compute number of bytes to copy
        const size_t offset = (counter - 1) * HASH_LEN;
        const size_t to_copy = (out_len - offset) < HASH_LEN ? (out_len - offset) : HASH_LEN;
        memcpy(out + offset, block, to_copy);
    }

#ifdef CONFIG_ESPTAG_ZEROIZE
    mbedtls_platform_zeroize(in_buf, sizeof(in_buf));
    mbedtls_platform_zeroize(block, sizeof(block));
#endif  // CONFIG_ESPTAG_ZEROIZE

    return rc;
}

static status_t compute_d(const uint8_t d_0[D_LEN], const uint8_t u[UV_LEN], const uint8_t v[UV_LEN],
                          uint8_t d_i[D_LEN])
{
    // Prepare for bignum and EC arithmetic
    // Disclaimer: goto's are used here to avoid repeating & nesting code
    mbedtls_mpi n, mu, mv, acc;
    mbedtls_mpi_init(&n);
    mbedtls_mpi_init(&mu);
    mbedtls_mpi_init(&mv);
    mbedtls_mpi_init(&acc);

    status_t rc = STATUS_ERR;
    // Read n (P-224 curve order)
    if (mbedtls_mpi_read_binary(&n, P224_N, sizeof(P224_N)) != 0) {
        ESP_LOGE(LOG_TAG, "curve order not read");
        goto out;
    }

    // Read u_i
    if (mbedtls_mpi_read_binary(&mu, u, UV_LEN) != 0) {
        ESP_LOGE(LOG_TAG, "u_i not read");
        goto out;
    }

    // Read v_i
    if (mbedtls_mpi_read_binary(&mv, v, UV_LEN) != 0) {
        ESP_LOGE(LOG_TAG, "v_i not read");
        goto out;
    }

    // Read acc = d_0
    if (mbedtls_mpi_read_binary(&acc, d_0, D_LEN) != 0) {
        ESP_LOGE(LOG_TAG, "d_0 not read");
        goto out;
    }

    // Do calculations
    // acc = acc * u
    if (mbedtls_mpi_mul_mpi(&acc, &acc, &mu) != 0) {
        ESP_LOGE(LOG_TAG, "mpi mul failed");
        goto out;
    }
    // acc = acc mod n
    if (mbedtls_mpi_mod_mpi(&acc, &acc, &n) != 0) {
        ESP_LOGE(LOG_TAG, "mpi mod failed");
        goto out;
    }
    // acc = acc + v
    if (mbedtls_mpi_add_mpi(&acc, &acc, &mv) != 0) {
        ESP_LOGE(LOG_TAG, "mpi add failed");
        goto out;
    }
    // acc = acc mod n
    if (mbedtls_mpi_mod_mpi(&acc, &acc, &n) != 0) {
        ESP_LOGE(LOG_TAG, "mpi mod failed");
        goto out;
    }

    // Make sure acc is non-zero
    // Skipping check because it's too unlikely (~2^(-224) chance)
    /*
    if (mbedtls_mpi_cmp_int(&acc, 0) == 0) {
        ESP_LOGE(LOG_TAG, "d_i is zero!");
        goto out;
    }
    */

    // Copy to output buffer
    if (mbedtls_mpi_write_binary(&acc, d_i, D_LEN) != 0) {
        ESP_LOGE(LOG_TAG, "d_i not written");
        goto out;
    }

    rc = STATUS_OK;

out:
    mbedtls_mpi_free(&n);
    mbedtls_mpi_free(&mu);
    mbedtls_mpi_free(&mv);
    mbedtls_mpi_free(&acc);
    return rc;
}
