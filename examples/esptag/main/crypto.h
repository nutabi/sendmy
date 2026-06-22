#ifndef ESPTAG_CRYPTO
#define ESPTAG_CRYPTO

#include <stdint.h>

#include "common.h"

// SHA-256 / KDF block length
#define HASH_LEN 32

// P-224 EC (SECP224R1) group order length
#define N_LEN 28

// P-224 EC (SECP224R1) group order
extern const uint8_t P224_N[N_LEN];

/**
 * Initialize the crypto core
 *
 * This brings up PSA and select the secp224r1 curve. Call once at startup
 * before any other functions in this module.
 *
 * @return          Status code
 */
status_t crypto_init(void);

/**
 * Rotate the symmetric key for a number of times
 *
 * @param sk_0      Seed symmetric key
 * @param counter   Number of rotations to apply
 * @param sk_i      Target symmetric key
 * @return          Status code
 */
status_t crypto_advance_sk(const uint8_t sk_0[SK_LEN],
                      uint32_t counter,
                      uint8_t sk_i[SK_LEN]);

/**
 * Rotate the symmetric key once
 *
 * This follows the formula: sk_next = KDF(sk_prev, "update", 32)
 *
 * @param sk_prev   Previous symmetric key
 * @param sk_next   Next symmetric key
 * @return          Status code
 */
status_t crypto_update_sk(const uint8_t sk_prev[SK_LEN],
                     uint8_t sk_next[SK_LEN]);

/**
 * Derive advertising public scalar from private scalar and symmetric key
 *
 * This computes (u || v) = KDF(sk_i, "diversify", 72), then d_i = d_0 * u + v.
 * p_i is finally derived from d_i through EC point multiplication, compressed
 * and with header byte stripped.
 *
 * @param d_0       Seed private scalar
 * @param sk_i      Current symmetric key
 * @param p_i       Compressed and stripped public scalar
 * @return          Status code
 */
status_t crypto_derive_p(const uint8_t d_0[D_LEN],
                    const uint8_t sk_i[SK_LEN],
                    uint8_t p_i[P_LEN]);

#endif // ESPTAG_CRYPTO
