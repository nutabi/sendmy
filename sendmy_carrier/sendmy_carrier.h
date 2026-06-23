#ifndef SENDMY_CARRIER_H
#define SENDMY_CARRIER_H

#include "esp_err.h"

#include <stdint.h>

#define SM_CR_UID_LEN 32
#define SM_CR_INFO_LEN 4
#define SM_CR_CARRIER_LEN 28

esp_err_t sm_cr_build_carrier(const uint8_t uid[SM_CR_UID_LEN], uint32_t mid, uint8_t payload,
                              uint8_t carrier[SM_CR_CARRIER_LEN]);

#endif  // SENDMY_CARRIER_H
