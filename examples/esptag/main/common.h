/**
 * common.h
 *
 * Stores shared symbols that multiple units depend on.
 */

#ifndef ESPTAG_COMMON
#define ESPTAG_COMMON

/*
 * Status code returned by every public function in this firmware (and the
 * internal helpers that follow the same convention): STATUS_OK on success,
 * STATUS_ERR on any failure. No error *code* is propagated to the caller; the
 * failing function logs the specific esp_err_t / PSA / NimBLE status at the
 * failure point (ESP_LOGE) and collapses it to STATUS_ERR, and callers treat a
 * non-STATUS_OK result as fatal. Keep new code to this enum rather than
 * returning esp_err_t. Programmer-error preconditions that cannot vary at
 * runtime use assert() instead (see the kdf checks in crypto.c).
 *
 * One exception, deliberately inverted: crypto.c's uecc_rng must follow
 * micro-ecc's RNG contract (1 = success) and stays a plain int — it is
 * commented at the call site.
 *
 * Note this convention is firmware-internal. Reusable IDF components live
 * outside it and return esp_err_t (the platform-wide type); their callers in
 * this firmware collapse that to status_t at the boundary (see ble_adv.c over
 * the sendmy_link component).
 */
typedef enum {
    STATUS_OK  = 0,
    STATUS_ERR = 1,
} status_t;

// Symmetric key length
#define SK_LEN 32

// P-224 EC private scalar length
#define D_LEN  28

// Advertising key (P-224 EC public scalar) length
#define P_LEN  28

#endif // ESPTAG_COMMON
