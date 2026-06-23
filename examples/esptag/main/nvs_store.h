#ifndef ESPTAG_NVS_STORE
#define ESPTAG_NVS_STORE

#include "common.h"  // D_LEN, SK_LEN

#include <stdint.h>

/**
 * @brief Initialise the default NVS flash partition.
 *
 * Call once at startup before any load/save. Any failure (including a corrupt
 * partition) is fatal; recovery means re-flashing.
 *
 * @return STATUS_OK on success, STATUS_ERR on any failure.
 */
status_t nvs_store_init(void);

/**
 * @brief Load the provisioned seed (d_0, sk_0) from NVS into caller buffers.
 *
 * The seed is written to the NVS partition at flash time from seed.csv; the
 * firmware never writes it back. This loads the raw seed scalars only; pass them
 * to tag_init() (via a tag_t) to derive the rest of the runtime state. Takes
 * plain byte buffers rather than a tag_t so nvs_store stays decoupled from the
 * tag layout.
 *
 * @param d_0  Out: the seed private scalar (D_LEN bytes).
 * @param sk_0 Out: the seed symmetric key (SK_LEN bytes).
 * @return STATUS_OK on success; STATUS_ERR if the namespace or either key is
 *         absent (device not provisioned), in which case the caller should not
 *         continue.
 */
status_t nvs_store_load_seed(uint8_t d_0[D_LEN], uint8_t sk_0[SK_LEN]);

/**
 * @brief Load the persisted rotation counter from the writable state namespace.
 *
 * A missing namespace/key reads back as 0 (first boot since provisioning), not
 * an error. Used at boot to fast-forward the ratchet (crypto_advance_sk) so
 * identifiers do not replay across reboots.
 *
 * @param counter Out: the persisted counter, or 0 if none is stored yet.
 * @return STATUS_OK on success (including the absent case), STATUS_ERR only on
 *         an unexpected NVS error.
 */
status_t nvs_store_load_counter(uint32_t *counter);

/**
 * @brief Persist the rotation counter to the writable state namespace.
 *
 * Opens the namespace, sets the key, and commits. Called after each epoch
 * advance when counter persistence is enabled.
 *
 * @param counter The counter value to store.
 * @return STATUS_OK on success, STATUS_ERR on any NVS failure.
 */
status_t nvs_store_save_counter(uint32_t counter);

#endif  // ESPTAG_NVS_STORE
