#ifndef SENDMY_LINK_H
#define SENDMY_LINK_H

#include "esp_err.h"

#include <stdint.h>

/**
 * Length (in byte) of the advertising key
 */
#define SM_LL_KEY_LEN 28

/**
 * @brief   Initialise the Send My link layer
 *
 * It basically sets up all the NimBLE parameters, mutex, etc.
 *
 * @param   on_ready            Callback when the NimBLE stack is up and ready
 * @param   adv_interval_ms     How often to advertise (in milliseconds)
 * @return  ESP standard return conventions
 */
esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms);

/**
 * @brief   Set the next advertising key
 *
 * It safely writes the new key to a buffer then alerts the NimBLE host task
 * to apply the key and restart advertising.
 *
 * @param   key     28-byte payload
 * @return  ESP standard return conventions
 */
esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN]);

#endif  // SENDMY_LINK_H
