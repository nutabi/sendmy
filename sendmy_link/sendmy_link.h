#ifndef SENDMY_LINK_H
#define SENDMY_LINK_H

#include "esp_err.h"

#include <stdint.h>

/**
 * Length (in byte) of the advertising key
 */
#define SM_LL_KEY_LEN 28

/**
 * Default advertising interval (in milliseconds) applied by sm_ll_init until a
 * caller overrides it with sm_ll_set_adv_interval().
 */
#define SM_LL_DEFAULT_ADV_INTERVAL_MS 1000

/**
 * @brief   Initialise the Send My link layer
 *
 * It basically sets up all the NimBLE parameters, mutex, etc. The advertising
 * interval starts at SM_LL_DEFAULT_ADV_INTERVAL_MS; call sm_ll_set_adv_interval()
 * afterwards to change it.
 *
 * @param   on_ready            Callback when the NimBLE stack is up and ready
 * @return  ESP standard return conventions
 */
esp_err_t sm_ll_init(void (*on_ready)(void));

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

/**
 * @brief   Set the BLE advertising TX power
 *
 * Controls the radiated power of the advertising events, which trades range
 * (how far a relay can be and still hear the carrier) against power draw. Call
 * after sm_ll_init(); the setting is held at the controller and persists across
 * the per-window advertising restarts. If never called, the controller default
 * applies.
 *
 * The ESP32 BLE controller quantises TX power to 3 dBm steps over roughly
 * [-24, +20] dBm; the requested value is rounded to the nearest step and
 * clamped to that range. The module/antenna may cap the effective radiated
 * power below the requested setting.
 *
 * @param   dbm     Desired TX power in dBm (rounded to the nearest 3 dBm step)
 * @return  ESP standard return conventions
 */
esp_err_t sm_ll_set_tx_power(int8_t dbm);

/**
 * @brief   Set the BLE advertising interval
 *
 * Controls how often a legacy advertising event fires, i.e. how frequently the
 * current key is broadcast. Call after sm_ll_init(); the value takes effect on
 * the next advertisement (every sm_ll_set_key() re-applies it), so set it before
 * publishing a key. If never called, SM_LL_DEFAULT_ADV_INTERVAL_MS applies.
 *
 * BLE legacy advertising uses 0.625 ms units; the valid HCI range is
 * 20..10240 ms and out-of-range values are rejected.
 *
 * @param   adv_interval_ms     Desired advertising interval in milliseconds
 * @return  ESP standard return conventions
 */
esp_err_t sm_ll_set_adv_interval(uint32_t adv_interval_ms);

#endif  // SENDMY_LINK_H
