#ifndef SENDMY_TRANSPORT_H
#define SENDMY_TRANSPORT_H

#include <stdint.h>

#include "esp_err.h"

#define SM_TL_UID_LEN     32
#define SM_TL_INFO_LEN    4
#define SM_TL_CID_LEN     27
#define SM_TL_CARRIER_LEN (SM_TL_CID_LEN + 1)

esp_err_t sm_tl_build_carrier(const uint8_t uid[SM_TL_UID_LEN],
                              uint32_t      mid,
                              uint8_t       payload,
                              uint8_t       carrier[SM_TL_CARRIER_LEN]);

#endif // SENDMY_TRANSPORT_H
