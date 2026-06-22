# espsend

`espsend` is an end-to-end demo of the `sendmy` data channel. It uses both
`sendmy_carrier` and `sendmy_link` to transmit payload bytes over Apple's Find
My network.

---

## What it does

On startup it loads a 32-byte *Unilink ID* (`uid`) from NVS, then runs a loop
that increments a 32-bit message ID forever. For each message ID it derives a
28-byte carrier via `sm_cr_build_carrier`, hands it to `sm_ll_set_key`, and
waits 60 seconds before advancing. The payload for message ID `mid` is
`1 << (mid % 8)`, so it cycles through one-hot bytes `0x01 0x02 0x04 ... 0x80`
repeating every 8 messages.

The 60-second window is demo-friendly. A real deployment would need a longer
window (several minutes) to give Apple's relay network enough time to observe
the beacon and file a report.

```
mid=0  payload=0x01  carrier = CID(uid, 0) || 0x01  ->  advertised 60s
mid=1  payload=0x02  carrier = CID(uid, 1) || 0x02  ->  advertised 60s
...
mid=7  payload=0x80  carrier = CID(uid, 7) || 0x80  ->  advertised 60s
mid=8  payload=0x01  (wraps, new CID)               ->  advertised 60s
...
```

---

## Provisioning

The `uid` is a 32-byte shared secret that must be flashed to the device before
anything else. Place a 64-hex-char `uid.hex` file at the project root
(one line, no newline required), which is also produced by a Python script:

```sh
# generate one
scripts/gen_seed.py
```

The top-level `CMakeLists.txt` calls `scripts/gen_nvs_csv.py` at build time
to convert `uid.hex` into an NVS partition image, which `idf.py flash` writes
to the `nvs` partition.

```sh
idf.py build
idf.py flash monitor
```

The receiver also needs `uid.hex` — it is the shared secret that makes the
channel work.

---

## Carrier derivation

The carrier for `(uid, mid, payload)` is:

```
info    = 0x73 0x6D 0x76 0x31 || mid_be32   ("smv1" in ASCI + mid in big-endian)
CID     = HMAC-SHA256(key=uid, data=info || 0x01)[:27]
carrier = CID || payload
```

This is the first T(1) block of HKDF-Expand (RFC 5869) truncated to 27 bytes,
with `uid` acting as the pseudo-random key. The implementation
(`sendmy_carrier.c`) uses the PSA Crypto API. Key material is scrubbed with
`mbedtls_platform_zeroize` on every exit path.

The CID is deterministic: anyone with `uid` and `mid` can recompute it.
Without `uid`, the carrier looks like a random 28-byte string.

---

## Host-side receiver

`scripts/fetch_reports.py` recovers a transmitted byte for a given `mid`:

```sh
# first login (saves session to account.json)
scripts/.venv/bin/python scripts/fetch_reports.py --message-id 0
# subsequent runs reuse the saved session
scripts/.venv/bin/python scripts/fetch_reports.py --message-id 3
```

For the given `mid` it builds all 256 candidate carriers (`CID || 0x00` through
`CID || 0xFF`), hashes each with SHA-256, and queries Apple's location
endpoint. The hash that comes back with a report identifies the transmitted
byte. Output is printed to stdout as a two-hex-char octet, so it can be piped.

Apple keeps reports for 7 days, so you can fetch old messages retroactively.

### scan_findmy.py

`scripts/scan_findmy.py` is a local BLE scanner that shows what the device is
actually advertising. It uses the `findmy` Python library (Bleak under the
hood) and parses the offline-finding frame to reconstruct `p_curr`. Useful for
confirming the firmware is broadcasting before waiting for Apple's servers to
pick it up.

```sh
scripts/.venv/bin/python scripts/scan_findmy.py           # 30s scan
scripts/.venv/bin/python scripts/scan_findmy.py -d 0      # until Ctrl-C
```

On macOS, the terminal running the script needs Bluetooth permission.

---

## Notes

- The `uid` serves as a symmetric key for the whole channel. Anyone who obtains
it can both read and impersonate transmissions.
- There is no freshness guarantee — a recorded carrier can be replayed.
- The channel capacity is one octet per advertising window, which at
60 s/window is about 0.13 bits/second. That is a feature of the underlying
Apple infrastructure, not something fixable in firmware.
