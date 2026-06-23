# `espsend`

`espsend` is the end-to-end demo of the `sendmy` data channel. It wires
`sendmy_carrier` and `sendmy_link` together to push payload bytes out over
Apple's Find My network, one octet at a time.

## What it does

On boot it loads a 32-byte Unilink ID (`uid`) from NVS, then loops forever over
a monotonic 32-bit message ID. For each `mid` it derives a carrier with
`sm_cr_build_carrier`, hands it to `sm_ll_set_key`, and advertises for 60
seconds before moving on. The payload is `1 << (mid % 8)`, so it walks a single
one-hot bit, `0x01 0x02 0x04 ... 0x80`, repeating every eight messages.

```
mid=0  payload=0x01  carrier = HKDF(uid, 0, 0x01)[:28]   advertised 60s
mid=1  payload=0x02  carrier = HKDF(uid, 1, 0x02)[:28]   advertised 60s
...
mid=7  payload=0x80  carrier = HKDF(uid, 7, 0x80)[:28]   advertised 60s
mid=8  payload=0x01  carrier = HKDF(uid, 8, 0x01)[:28]   advertised 60s
```

The 60-second window is deliberately short so the demo is watchable. A real
deployment would give each octet several minutes, since throughput is capped by
how often Apple's relay network bothers to file a report (very roughly one octet
per minute, or about 0.13 bits per second). That is a property of Apple's
infrastructure, not something the firmware can speed up.

## Provisioning

The `uid` is the shared secret for the whole channel and must be flashed before
the firmware can do anything. Drop a 64-hex-character `uid.hex` (32 bytes, one
line) at the project root, or generate one:

```sh
scripts/gen_seed.py
```

At build time the top-level `CMakeLists.txt` runs `scripts/gen_nvs_csv.py` to
turn `uid.hex` into an NVS image, and `idf.py flash` writes it to the `nvs`
partition. The receiver needs the same `uid.hex`; without it the channel is just
noise.

## Build and flash

The example targets the Seeed XIAO ESP32-S3 (esp32s3, 8 MB flash).

```sh
idf.py build
idf.py flash monitor
```

## Host-side scripts

`scripts/fetch_reports.py` is the receiver. Given a message ID it builds all 256
candidate carriers, one per possible payload octet, hashes each with SHA-256, and
queries Apple's location endpoint; the hash that comes back with a report is the
transmitted byte, printed as two hex chars so you can pipe it. Each candidate is
derived exactly as the firmware does — the x-coordinate of `d*G` for a scalar `d`
HKDF'd from `(uid, mid, payload)` — so it matches the advertised key byte-for-byte.

```sh
# first run logs in and saves the session to account.json
scripts/.venv/bin/python scripts/fetch_reports.py --message-id 0
scripts/.venv/bin/python scripts/fetch_reports.py --message-id 3
```

Apple keeps reports for seven days, so you can fetch old messages well after the
fact.

Because every carrier is now `X(d*G)` — a valid P-224 point by construction — a
"no carrier present" result from `fetch_reports.py` means the message was lost in
transit, not that the carrier was an undeliverable point.

`scripts/scan_findmy.py` is a local BLE scanner for sanity-checking that the
device is actually broadcasting before you wait on Apple's servers. It parses
the OF frame and reconstructs the advertised key.

```sh
scripts/.venv/bin/python scripts/scan_findmy.py        # 30s scan
scripts/.venv/bin/python scripts/scan_findmy.py -d 0   # until Ctrl-C
```

On macOS the terminal running it needs Bluetooth permission.

## Notes

- `uid` is a symmetric secret: anyone holding it can both read transmissions and
  forge them.
- There is no freshness, so a recorded carrier can be replayed.
- `uid.hex` is the master secret. Keep it out of version control.
