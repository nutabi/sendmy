# `espsend`

`espsend` is the end-to-end demo of the `sendmy` data channel. It wires
`sendmy_carrier` and `sendmy_link` together to push payload bytes out over the
crowd-sourced BLE relay network, one octet at a time.

## What it does

On boot it loads a 32-byte Unilink ID (`uid`) from NVS and brings up a BME280
temperature sensor on I2C (SDA = GPIO5, SCL = GPIO6, default address `0x76`
since the sensor's SDO pin is left floating). It then loops forever over a
monotonic 32-bit message ID. For each `mid` it wakes the sensor for a single
forced measurement, reads the temperature, encodes it into the single payload
octet, derives a carrier with `sm_cr_build_carrier`, hands it to `sm_ll_set_key`,
and advertises for 60 seconds before moving on.

The BME280 is kept in forced mode for power: it sleeps between key rotations and
takes exactly one measurement per `mid` (temperature only, x1 oversampling),
returning to sleep on its own. The stock `bme280_default_init` would instead run
the sensor continuously in NORMAL mode, so the example overrides that.

The payload is the temperature rounded to the nearest whole degree Celsius and
transmitted as a two's-complement signed byte (`-128..127` C), which spans any
realistic ambient reading.

```
mid=0  temp=24.6C  payload=0x19 (25)   d = HKDF(uid, 0, 0x19); carrier = X(d*G)   advertised 60s
mid=1  temp=24.7C  payload=0x19 (25)   d = HKDF(uid, 1, 0x19); carrier = X(d*G)   advertised 60s
mid=2  temp=25.4C  payload=0x19 (25)   d = HKDF(uid, 2, 0x19); carrier = X(d*G)   advertised 60s
...
```

The 60-second window is deliberately short so the demo is watchable. A real
deployment would give each octet several minutes, since throughput is capped by
how often the relay network bothers to file a report (very roughly one octet
per minute, or about 0.13 bits per second). That is a property of the relay
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
queries the relay's location endpoint; the hash that comes back with a report is the
transmitted byte, printed as two hex chars so you can pipe it. That byte is the
signed temperature in degrees Celsius (e.g. `0x19` is 25 C, `0xfb` is -5 C). Each
candidate is derived exactly as the firmware does — the x-coordinate of `d*G` for a scalar `d`
HKDF'd from `(uid, mid, payload)` — so it matches the advertised key byte-for-byte.

```sh
# first run logs in and saves the session to account.json
scripts/.venv/bin/python scripts/fetch_reports.py --message-id 0
scripts/.venv/bin/python scripts/fetch_reports.py --message-id 3
```

The relay keeps reports for seven days, so you can fetch old messages well after
the fact.

Because every carrier is now `X(d*G)` — a valid P-224 point by construction — a
"no carrier present" result from `fetch_reports.py` means the message was lost in
transit, not that the carrier was an undeliverable point.

`scripts/scan_findmy.py` is a local BLE scanner for sanity-checking that the
device is actually broadcasting before you wait on the relay servers. It parses
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
