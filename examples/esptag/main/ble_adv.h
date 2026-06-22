#ifndef ESPTAG_BLE_ADV
#define ESPTAG_BLE_ADV

#include "tag.h"

/**
 * @brief Drive Find My advertising from a rotating tag identity.
 *
 * The esptag driver on top of the sendmy_link "link layer": it brings up
 * advertising for the tag's current key, and (when CONFIG_ESPTAG_ROTATE_ENABLE
 * is set) arms a timer that rotates the tag every epoch
 * (CONFIG_ESPTAG_ROTATE_INTERVAL_MS), persists the new counter
 * (CONFIG_ESPTAG_PERSIST_COUNTER), and re-broadcasts under the fresh key. The
 * Find My frame construction, BLE address, and NimBLE stack all live in
 * sendmy_link; this layer only owns the tag/rotation/persistence policy.
 *
 * The tag pointer is retained: the host task reads p_curr and rotates the tag
 * after ble_adv_init returns. So the tag must outlive this module. Pass a struct
 * with static/global lifetime, not a stack local.
 *
 * @param tag Tag driving the advertised identifier; must outlive this module.
 * @return STATUS_OK on success, STATUS_ERR on failure.
 */
status_t ble_adv_init(tag_t *tag);

#endif // ESPTAG_BLE_ADV
