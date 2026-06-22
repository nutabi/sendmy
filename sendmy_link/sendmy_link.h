#ifndef SENDMY_LINK_H
#define SENDMY_LINK_H

#include <stdint.h>

#include "esp_err.h"

/*
 * sendmy_link — the link layer of the sendmy protocol.
 *
 * Broadcasts a single 28-byte key as a valid Apple offline-finding ("Find My"-
 * style) BLE advertisement, owning the NimBLE stack, the `1e ff 4c 00`
 * manufacturer frame, and the rotating BLE random address. It is agnostic to
 * what the key means — anything that produces 28-byte keys can drive it. See
 * README.md for the frame format, the key-byte budget, the threading model, and
 * usage.
 */

// The key length broadcast per advertisement — the link MTU. See README.md.
#define SM_LL_KEY_LEN 28

/**
 * @brief Bring up the NimBLE stack and spawn the host task.
 *
 * Advertising does not begin until a key is supplied; a key buffered before the
 * stack syncs is broadcast on sync.
 *
 * @param on_ready        Optional (may be NULL) callback fired once, on the host
 *                        task, when the stack first syncs and is ready to advertise.
 * @param adv_interval_ms Advertising interval in milliseconds (itvl_min == itvl_max).
 * @return ESP_OK, or the failing esp_err_t (e.g. from nimble_port_init).
 */
esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms);

/**
 * @brief Set the key to broadcast.
 *
 * Safe to call from any task. The new key/address is applied asynchronously on
 * the host task by the next advertising cycle; concurrent calls coalesce to the
 * most recent key.
 *
 * @param key The SM_LL_KEY_LEN-byte key to broadcast.
 * @return ESP_OK once buffered and queued, ESP_ERR_INVALID_ARG (NULL key), or
 *         ESP_ERR_INVALID_STATE (called before sm_ll_init). A GAP failure during
 *         the asynchronous apply is logged, not returned.
 */
esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN]);

#endif // SENDMY_LINK_H
