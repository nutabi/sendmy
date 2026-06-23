# `esptag`

`esptag` is a self-contained Find My tag, an AirTag clone in everything but the
pairing. It broadcasts valid Offline Finding advertisements through
`sendmy_link` and runs its own P-224 key ratchet on top, mirroring the algorithm
Apple specifies for Find My accessories. It does not use `sendmy_carrier`; it is
a tag, not a data channel.

You cannot pair it with the Find My app, but it still emits real OF beacons, and
anyone holding the provisioned root secrets can reconstruct the per-epoch
private keys and decrypt the location reports Apple's network collected.

## How the key ratchet works

The tag keeps its crypto state in a single struct:

```c
typedef struct {
    uint8_t  d_0[28];     // seed private scalar (from NVS)
    uint8_t  sk_0[32];    // seed symmetric key (from NVS)
    uint8_t  sk_curr[32]; // current ratchet key
    uint32_t counter;     // current epoch index
    uint8_t  p_curr[28];  // advertising public key for this epoch
} tag_t;
```

Every *epoch* it advances one step:

1. Ratchet the symmetric key: `sk_i = KDF(sk_{i-1}, "update", 32)`.
2. Derive the epoch scalar: `(u || v) = KDF(sk_i, "diversify", 72)`, then
   `d_i = (d_0 * u + v) mod n`.
3. Compute the advertising key: `p_i = compress(d_i * G)[1:]`, the compressed
   secp224r1 public key with its header byte stripped, leaving 28 bytes.

`p_i` goes to `sm_ll_set_key`, which rotates the BLE identity. The `KDF` is ANSI
X9.63, the same one AirTags use. The scalar arithmetic runs on mbedTLS's
`mbedtls_mpi`; the point multiplication `d_i * G` uses the vendored `micro_ecc`
configured for secp224r1.

## Provisioning

The root identity is a 32-byte master seed. Generate it and expand it once:

```sh
python3 scripts/gen_seed.py      # 32-byte master seed -> seed.key
python3 scripts/derive_keys.py   # seed.key -> seed.csv + seed.keys
```

`gen_seed.py` uses `os.urandom`, or derives the seed from a passphrase
(`--passphrase`) for a reproducible test identity. `derive_keys.py` expands the
seed into the two roots via HKDF-SHA256:

- `sk_0`, the 32-byte initial symmetric key.
- `d_0`, the 28-byte initial private scalar, rejection-sampled to stay uniform
  in `[1, n-1]` without modulo bias. (The reject path is effectively never
  taken; the odds of overshooting are about `2^-112`.)

It writes `seed.csv` (the NVS image `idf.py flash` embeds) and `seed.keys` (the
plaintext roots for the host-side fetch script).

## Build and flash

The example targets the Seeed XIAO ESP32-S3 (esp32s3, 8 MB flash).

```sh
idf.py build
idf.py flash monitor
```

Flashing rewrites the whole `nvs` partition, which also wipes the writable
counter, so the tag restarts at epoch 0 as if freshly provisioned. A plain
reboot keeps the counter.

## NVS layout

Two namespaces, kept separate on purpose:

- `esptag` holds the read-only seed: `d_0` (28-byte blob) and `sk_0` (32-byte
  blob).
- `esptag_st` holds the runtime-writable `counter` (u32), the current epoch.

Splitting them means re-flashing the seed image never collides with a
partially-written counter, and the counter write path never touches the seed.

## Rotation and persistence

Rotation is driven by a NimBLE callout on the host task, every 15 minutes by
default, matching the Find My cadence. With `CONFIG_ESPTAG_PERSIST_COUNTER` on
(the default) the counter is written to NVS after each rotation; on the next
boot the tag fast-forwards the ratchet from `sk_0` by that many steps and
resumes where it left off instead of replaying from epoch 0. If a counter write
keeps advertising and retries next epoch, so a bad write costs a few replayed
epochs, never a silent death. With persistence off the counter always starts at
0, which is handy for reproducible testing.

## Kconfig options

All under *esptag configuration* in `idf.py menuconfig`:

- `ESPTAG_ROTATE_ENABLE` (default y): enable key rotation; disable to hold a
  static identity for testing.
- `ESPTAG_ROTATE_INTERVAL_MS` (default 900000, 15 min): how often an epoch
  advances. Drop to a few seconds to watch it rotate.
- `ESPTAG_ADV_INTERVAL_MS` (default 2000): BLE advertising interval; both
  `itvl_min` and `itvl_max` are pinned to it.
- `ESPTAG_PERSIST_COUNTER` (default y): persist the counter so identities do not
  replay after a reboot.
- `ESPTAG_ZEROIZE` (default y): scrub intermediate key material with
  `mbedtls_platform_zeroize`.
- `ESPTAG_OWN_LOG_LEVEL` (default Info): log level for the tag's own modules;
  IDF and NimBLE stay at Warn.

## Host-side scripts

`scripts/scan_findmy.py` confirms the tag is advertising locally. It parses the
OF frame and prints the 28-byte `p_curr` the firmware logged at the last
rotation.

```sh
scripts/.venv/bin/python scripts/scan_findmy.py             # 30s scan
scripts/.venv/bin/python scripts/scan_findmy.py -d 0        # continuous
scripts/.venv/bin/python scripts/scan_findmy.py --rssi -70  # wider range
```

`scripts/fetch_reports.py` pulls location reports from Apple for a range of
epochs. For each epoch it derives the private scalar with the same
`d_i = (d_0 * u + v) mod n` the firmware uses, wraps it in a key pair, and
queries Apple in one batched request. Reports print oldest-first with epoch
index, timestamp, lat/lon, and accuracy.

```sh
# first run logs in and saves the session next to the script
scripts/.venv/bin/python scripts/fetch_reports.py --email you@icloud.com
scripts/.venv/bin/python scripts/fetch_reports.py
# cover the full 7-day window for a 15-minute interval
scripts/.venv/bin/python scripts/fetch_reports.py --interval-ms 900000
# derive and print epoch keys only, no network
scripts/.venv/bin/python scripts/fetch_reports.py --key-only --epochs 4
```

`esptag_const.py` holds the constants (curve order, key sizes, NVS names) that
the script and firmware have to agree on.

## Security notes

The root secret (`d_0`, `sk_0`) sits in the `nvs` partition in plaintext. The
target ESP32-S3R8 module has no secure element, so an `esptool read_flash` dump
recovers it. A hardened build would want Flash Encryption (release mode), Secure
Boot v2, and NVS encryption for the `esptag` namespace, with USB-JTAG disabled
by eFuse; even then a determined invasive attack can still read the key out of
RAM. This is a known limitation, accepted for the current research scope.

`seed.key`, `seed.keys`, and `seed.csv` all hold the root secret in plaintext.
They are written `0600` and must never be committed.
