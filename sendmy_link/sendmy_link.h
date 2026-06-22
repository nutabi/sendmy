#ifndef SENDMY_LINK_H
#define SENDMY_LINK_H

#include <stdint.h>

#include "esp_err.h"

#define SM_LL_KEY_LEN 28

esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms);

esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN]);

#endif // SENDMY_LINK_H
