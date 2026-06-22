# esptag

`esptag` is a self-contained Find My tag implementation. It uses `sendmy_link`
for BLE broadcasting and implements its own P-224 key ratchet on top, mirroring
the algorithm Apple specifies for Find My accessories. Each *epoch* the tag
derives a fresh advertising key and rotates its BLE identity. A receiver with
the provisioned root secrets can reconstruct the private keys for any epoch
and decrypt the location reports Apple's network collected.

---

## How the key ratchet works

The tag's cryptographic state lives in a `tag_t` struct:

```c
typedef struct {
    uint8_t  d_0[28];     // seed private scalar (from NVS)
    uint8_t  sk_0[32];    // seed symmetric key (from NVS)
    uint8_t  sk_curr[32]; // current ratchet key
    uint32_t counter;     // current epoch index
    uint8_t  p_curr[28];  // advertising public key for this epoch
} tag_t;
```

At each epoch transition:

1. **Ratchet the symmetric key:** `sk_i = KDF(sk_{i-1}, "update", 32)`
2. **Derive the epoch's EC scalar:** `(u || v) = KDF(sk_i, "diversify", 72)`,
then `d_i = (d_0 × u + v) mod n`
3. **Compute the advertising key:** `p_i = compress(d_i × G)[1:]` — the
compressed secp224r1 public key, header byte stripped, giving 28 bytes.

`p_i` is passed to `sm_ll_set_key`, which rebuilds the random address and
advertisement payload and restarts advertising.

The `KDF` used here is ANSI X9.63, same as what is used in AirTag.

The EC arithmetic (`d_i = d_0 × u + v mod n`) is done via mbedTLS's
`mbedtls_mpi` API. The point multiplication `d_i × G` uses the vendored
`micro_ecc` library configured for secp224r1.

---

## Provisioning

The tag's root identity comes from a 32-byte master seed:

```sh
# run once from examples/esptag
python3 scripts/gen_seed.py
python3 scripts/derive_keys.py
```

`gen_seed.py` generates the master seed using `os.urandom`. It can also derive 
he seed deterministically from a passphrase (`--passphrase`) for reproducible
test identities.

`derive_keys.py` expands the seed into two root values via HKDF-SHA256:

- `sk_0` — 32-byte initial symmetric key: one HKDF-Expand block under label `"esptag sk_0"`.
- `d_0` — 28-byte initial private scalar: rejection-sampled HKDF-Expand blocks
under label `"esptag d_0"` + a 1-byte attempt counter, to ensure `d_0` is
uniform in `[1, n-1]` without modulo bias. (In practice the loop never iterate
— the probability of a sample landing outside `[1, n-1]` is roughly `2^-112`.)

`derive_keys.py` writes two files:

- `seed.csv` — NVS partition CSV that `idf.py flash` embeds into the `nvs`
partition.
- `seed.keys` — plaintext `d_0=<hex>` / `sk_0=<hex>` key file for the
host-side fetch script.

After running the two scripts, build and flash normally:

```sh
idf.py build
idf.py flash monitor
```

Flashing rewrites the whole `nvs` partition, which also wipes the
`esptag_st` namespace (the writable counter). The tag starts at epoch 0 again
— the same as a fresh provisioning. A plain reboot keeps the counter.

---

## NVS layout

Two NVS namespaces are used:

| Namespace | Key | Type | Contents |
|-----------|-----|------|----------|
| `esptag` | `d_0` | blob (28 B) | Seed private scalar (read-only) |
| `esptag` | `sk_0` | blob (32 B) | Seed symmetric key (read-only) |
| `esptag_st` | `counter` | u32 | Current epoch (writable at runtime) |

Keeping them separate means the seed image (`seed.csv`) never overlaps with the
runtime-writable counter. Re-flashing the seed image won't corrupt a partially-
written counter, and the counter write path never touches the seed namespace.

---

## Rotation and persistence

The rotation timer is a NimBLE callout that fires on the host task. Default
interval is 15 minutes (`CONFIG_ESPTAG_ROTATE_INTERVAL_MS = 900000`), matching
the Find My cadence. The interval is configurable via
`idf.py menuconfig`, then *esptag configuration*.

When `CONFIG_ESPTAG_PERSIST_COUNTER` is enabled (default on), the counter is
written to NVS after each rotation. On the next boot, `tag_init` reads the
persisted counter and fast-forwards the ratchet from `sk_0` by that many steps
(`crypto_advance_sk`), so the tag resumes from where it left off rather than
replaying from epoch 0.

If the counter write fails (e.g. flash wear), the tag keeps advertising under
the new identity and tries again next epoch. A reboot after a failed write will
replay the last few epochs, not go dark.

When `CONFIG_ESPTAG_PERSIST_COUNTER=n` the counter always starts at 0. Useful
for testing because it is reproducible and avoids flash wear.

---

## Kconfig options

All options are under *esptag configuration* in `idf.py menuconfig`.

| Option | Default | Description |
|--------|---------|-------------|
| `ESPTAG_ROTATE_ENABLE` | y | Enable key rotation. Disable to hold a static identity (for testing). |
| `ESPTAG_ROTATE_INTERVAL_MS` | 900000 (15 min) | How often the tag advances an epoch. Drop to a few seconds to watch rotation. |
| `ESPTAG_ADV_INTERVAL_MS` | 2000 ms | BLE advertising interval. Both `itvl_min` and `itvl_max` are pinned to this value. |
| `ESPTAG_PERSIST_COUNTER` | y | Save the rotation counter to NVS so identifiers do not replay after a reboot. |
| `ESPTAG_ZEROIZE` | y | Scrub intermediate key material (scalars, ratchet state) with `mbedtls_platform_zeroize`. |
| `ESPTAG_OWN_LOG_LEVEL` | Info | Runtime log level for the tag's own modules; IDF/NimBLE stay at Warn. |

---

## Host-side tools

### scan_findmy.py

Confirms the tag is advertising locally over BLE. It parses the offline-finding frame and prints the full 28-byte `p_curr` that the firmware logged at the last rotation. Useful before querying Apple's servers.

```sh
scripts/.venv/bin/python scripts/scan_findmy.py           # 30s scan
scripts/.venv/bin/python scripts/scan_findmy.py -d 0      # continuous
scripts/.venv/bin/python scripts/scan_findmy.py --rssi -70  # wider range
```

The script re-execs itself under `scripts/.venv` automatically if not already in that environment.

### fetch_reports.py

Fetches location reports from Apple's servers for a range of epochs:

```sh
# first run: log in and save session
scripts/.venv/bin/python scripts/fetch_reports.py --email you@icloud.com

# subsequent runs reuse the saved session (account.json next to the script)
scripts/.venv/bin/python scripts/fetch_reports.py

# cover the full 7-day window for a 15-minute rotation interval
scripts/.venv/bin/python scripts/fetch_reports.py --interval-ms 900000

# just derive and print the epoch keys, no network
scripts/.venv/bin/python scripts/fetch_reports.py --key-only --epochs 4
```

For each epoch it derives the private scalar using the same `d_i = (d_0 × u + v) mod n` formula the firmware uses, wraps it in a `KeyPair` object, and queries Apple in a single batched request. Reports are printed sorted oldest-first with epoch index, timestamp, lat/lon, and accuracy.

The script mirrors `crypto.c` exactly: `update_sk` applies the "update" KDF step, `derive_private` does the `(u, v)` diversify step and the scalar arithmetic. `esptag_const.py` holds the shared constants (curve order, key sizes, NVS names) that both the script and firmware must agree on.

---

## Security notes

The tag's root secret (`d_0`, `sk_0`) lives in the `nvs` partition in plaintext. On the target ESP32-S3R8 module there is no secure element, so a flash dump via `esptool read_flash` recovers the root secret. A hardened deployment would need Flash Encryption (release mode) + Secure Boot v2 + NVS encryption for the `esptag` namespace. USB-JTAG would also need to be disabled via eFuse. Even with all of that, a determined invasive attack (decapping the chip) can still probe the key from RAM.

This is a known limitation that was accepted for the current research scope.

`seed.key`, `seed.keys`, and `seed.csv` all carry the root secret in plaintext. They are written with `0600` permissions and should not be committed to version control.
