#ifndef SENDMY_CARRIER_P224_H
#define SENDMY_CARRIER_P224_H

#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

/** Byte length of a P-224 scalar and field/coordinate element. */
#define P224_LEN 28

/**
 * @brief Test whether a candidate scalar lies in the valid P-224 range.
 *
 * Used by the carrier rejection-sampling loop to decide whether a freshly
 * derived block is an acceptable private scalar before it is multiplied by G.
 *
 * @param[in] d 28-byte big-endian candidate scalar.
 *
 * @return true if 1 <= d < n (n = group order), false otherwise.
 */
bool p224_scalar_in_range(const uint8_t d[P224_LEN]);

/**
 * @brief Compute the x-coordinate of d*G on NIST P-224 (secp224r1).
 *
 * Multiplies the standard base point G by the scalar @p d and returns the
 * 28-byte big-endian X coordinate of the resulting public point.
 *
 * @param[in]  d      28-byte big-endian scalar. REQUIRED to be in [1, n-1];
 *                    the caller (the carrier rejection-sampling loop) must
 *                    guarantee this range. The scalar is NOT reduced mod n.
 * @param[out] out_x  Receives the 28-byte big-endian X coordinate of d*G,
 *                    left-padded with leading zero bytes as needed.
 *
 * @return ESP_OK on success, ESP_FAIL otherwise.
 */
esp_err_t p224_base_mult_x(const uint8_t d[P224_LEN], uint8_t out_x[P224_LEN]);

#endif  // SENDMY_CARRIER_P224_H
