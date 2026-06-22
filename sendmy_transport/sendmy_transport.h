#ifndef SENDMY_TRANSPORT_H
#define SENDMY_TRANSPORT_H

#include <stdint.h>

#include "esp_err.h"

/*
 * sendmy_transport — the transport layer of the sendmy protocol.
 *
 * Encodes one octet of application data into a 28-octet "carrier" that the link
 * layer broadcasts as a Find My key. A receiver holding the shared Unilink ID
 * recovers the octet by asking Apple whether the carrier exists. See README.md
 * for the carrier format, the derivation, the payload model, and the threat
 * model.
 */

#define SM_TL_UID_LEN     32                    // Unilink ID — the pre-shared PRK
#define SM_TL_INFO_LEN    4                     // HKDF info domain-separation tag
#define SM_TL_CID_LEN     27                    // Carrier ID
#define SM_TL_CARRIER_LEN (SM_TL_CID_LEN + 1)   // 27-octet CID + 1-octet payload = 28

/**
 * @brief Build a 28-octet carrier from the Unilink ID, message id, and payload.
 *
 * carrier = HKDF-Expand(uid, INFO || mid_be32, 27) || payload. The first
 * SM_TL_CID_LEN octets are the Carrier ID; the last is the payload. See README.md.
 *
 * @param uid     Unilink ID (must have >= 128-bit entropy), SM_TL_UID_LEN octets.
 * @param mid     Zero-indexed message sequence number.
 * @param payload Single data octet.
 * @param carrier Output buffer, SM_TL_CARRIER_LEN octets.
 * @return ESP_OK on success, or an esp_err_t on a PSA/crypto failure.
 */
esp_err_t sm_tl_build_carrier(const uint8_t uid[SM_TL_UID_LEN],
                              uint32_t      mid,
                              uint8_t       payload,
                              uint8_t       carrier[SM_TL_CARRIER_LEN]);

#endif // SENDMY_TRANSPORT_H
