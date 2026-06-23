#ifndef ESPTAG_TAG
#define ESPTAG_TAG

#include "common.h"  // D_LEN, SK_LEN, P_LEN

#include <stdint.h>

/*
 * Secret-bearing runtime state for one tag.
 *
 * The layout is exposed (not an opaque handle) so callers can hold a tag_t with
 * static/global lifetime — main.c owns the single instance and the BLE host task
 * keeps a pointer to it (see ble_adv.h). But these fields are owned by the tag
 * layer: only tag_*() functions should mutate d_0/sk_0/sk_curr/counter. Other
 * layers populate the seed via nvs_store_load_seed() into tag.d_0/tag.sk_0 before
 * tag_init(), and read p_curr/counter for advertising; they must not ratchet or
 * derive by hand. tag_destroy() zeroizes the whole struct.
 */
typedef struct {
    uint8_t d_0[D_LEN];    // Seed private scalar (from NVS)
    uint8_t sk_0[SK_LEN];  // Seed symmetric key (from NVS)

    uint8_t sk_curr[SK_LEN];  // Ratchet key at the current epoch
    uint32_t counter;         // Current epoch / ratchet step count
    uint8_t p_curr[P_LEN];    // Advertising key for the current epoch
} tag_t;

/**
 * @brief Bring the runtime state up from a provisioned seed, resuming at
 *        `counter`.
 *
 * Assumes d_0 and sk_0 are already populated (e.g. by nvs_store_load_seed). Sets
 * tag->counter, fast-forwards sk_curr from sk_0 by `counter` ratchet steps
 * (crypto_advance_sk), and derives the matching advertising key p_curr. Pass the
 * persisted counter (nvs_store_load_counter) so identifiers resume rather than
 * replay across reboots; counter == 0 is the first-boot / fresh-seed case and
 * leaves sk_curr == sk_0. Call once before tag_rotate.
 *
 * @param tag     Tag with d_0 and sk_0 already loaded; the rest is filled in.
 * @param counter Epoch to resume at (0 for a fresh seed / first boot).
 * @return Status code (STATUS_OK on success).
 */
status_t tag_init(tag_t *tag, uint32_t counter);

/**
 * @brief Advance the tag by one epoch.
 *
 * Ratchets sk_curr one step (crypto_update_sk), derives the new advertising key
 * p_curr from it, and bumps tag->counter. On any failure the tag is left
 * unchanged.
 *
 * @param tag Tag to rotate; must have been through tag_init.
 * @return Status code (STATUS_OK on success).
 */
status_t tag_rotate(tag_t *tag);

/**
 * @brief Zeroize the whole tag struct (all key material).
 *
 * The scrubbing itself only happens when CONFIG_ESPTAG_ZEROIZE is set; otherwise
 * this just null-checks.
 *
 * @param tag Tag to wipe.
 * @return STATUS_OK on success, STATUS_ERR only if tag is NULL.
 */
status_t tag_destroy(tag_t *tag);

#endif  // ESPTAG_TAG
