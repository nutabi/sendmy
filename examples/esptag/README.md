# esptag

ESP-IDF firmware for the **ESP32-S3** implementing the cryptographic core of a
privacy-preserving BLE locator tag — a "Find My"-style design. The tag rotates a
symmetric key on a fixed cadence, derives a fresh per-epoch elliptic-curve
private scalar from it, and broadcasts the compressed **P-224** public key as a
rotating BLE advertising identifier. Because the broadcast identity changes every
epoch and successive identities are unlinkable without the root secret, a passive
observer cannot track the tag over time.

This repository is the firmware only. It is built for a course project (CP2107)
and is documented for my future self, my supervisor, and anyone evaluating the
work.

> ⚠️ **Research / coursework firmware.** It demonstrates the key-rotation and
> identity-derivation scheme; it is not a hardened product. In particular the
> root seed is flashed in plaintext (see [Provisioning](#provisioning)).

This README is the overview and the build/run instructions. For the *why* — the
ESP-IDF / FreeRTOS execution model, the NimBLE stack, the Find My packet format,
the crypto internals, and a per-module walkthrough — see the
**[Developer Guide](DEVELOPING.md)**.

## How it works

Each **epoch** the tag advances one step of a symmetric-key ratchet and derives a
new advertising key from it:

```
sk_{i+1} = KDF(sk_i, "update", 32)
(u, v)   = KDF(sk_i, "diversify", 72)
d_i      = d_0 · u + v   (mod n)
p_i      = compress(d_i · G)
```

- `d_0`, `sk_0` are the two **root secrets**, provisioned once at flash time.
- The KDF is a SHA-256, NIST-SP-800-108-style counter-mode construction; `G` and
  `n` are the generator and order of **secp224r1 (P-224)**. All byte arrays are
  big-endian.
- `p_i` is the compressed point with its 1-byte header stripped (28 bytes), and
  is what gets framed into the BLE advertisement.

Knowing only `p_i`, an observer cannot recover `d_0`/`sk_0` or link `p_i` to
`p_{i+1}`; the holder of the root secrets can recompute the whole sequence and
recognise the tag. The KDF construction, the modular-arithmetic / EC details, and
the secret-scrubbing discipline are documented in the
[Developer Guide → cryptographic core](DEVELOPING.md#6-the-cryptographic-core).

## Architecture

Layered modules under `main/`, plus a vendored ECC library in `components/`.
Every public function returns `status_t` (`STATUS_OK` / `STATUS_ERR`); the
specific `esp_err_t` / PSA / NimBLE error is logged at the failure site, not
propagated.

| Module | Responsibility |
|---|---|
| `crypto.{c,h}` | Stateless crypto primitives (`crypto_update_sk`, `crypto_advance_sk`, `crypto_derive_p`). The only module touching mbedTLS, PSA, and micro-ecc. |
| `tag.{c,h}` | Owns the secret-bearing `tag_t` state (`d_0`, `sk_0`, `sk_curr`, `counter`, `p_curr`). `tag_init` resumes from a seed at a given epoch; `tag_rotate` advances one epoch; `tag_destroy` zeroizes. Depends only on `crypto`. |
| `nvs_store.{c,h}` | Loads the provisioned seed from the read-only `esptag` NVS namespace; persists the rotation counter in a separate writable `esptag_st` namespace. Refuses to run if unprovisioned. |
| `ble_adv.{c,h}` | The only module touching NimBLE. Brings up the host task, starts non-connectable legacy advertising, and arms a callout that rotates the identity every interval. |
| `main.c` | `app_main`: `crypto_init` → `nvs_store_init` → `nvs_store_load_seed` → (`nvs_store_load_counter`) → `tag_init` → `ble_adv_init`. Holds the single file-scope `tag_t`. |
| `components/micro_ecc/` | Vendored [micro-ecc](https://github.com/kmackay/micro-ecc), built for secp224r1 with compressed points. Used only for scalar→point and point compression; all modular scalar arithmetic uses mbedTLS `mbedtls_mpi`. |

**BLE payload.** Each epoch the 28-byte key `p_curr` is split across the
advertisement: the first 6 bytes become the (rotating) BLE random address and the
rest goes into a 27-byte Apple "offline finding" (Find My) manufacturer payload,
framed as `1e ff 4c 00` + payload = 31 bytes (the legacy-adv maximum). The exact
byte layout, the address-rotation rationale, and the NimBLE bring-up are in the
[Developer Guide → offline finding](DEVELOPING.md#5-apple-offline-finding-find-my-in-detail)
and [→ NimBLE stack](DEVELOPING.md#4-the-nimble-stack).

> **Note:** P-224 (secp224r1) was dropped from mbedTLS 4.0 (shipped in IDF 6), so
> the curve math cannot be moved off micro-ecc onto mbedTLS ECP. The vendored
> library is load-bearing for this reason.

## Toolchain setup

This project targets **ESP-IDF 6.0** (tested on v6.0.1), installed via the
ESP-IDF Installation Manager (`eim`) under `~/.espressif/`.

On this machine `export.sh` alone does **not** produce a working environment (it
looks for the venv under the classic `idf_tools` layout and never puts `ninja` on
`PATH`). Use this in each new shell before any `idf.py` command:

```bash
export IDF_PATH=~/.espressif/v6.0.1/esp-idf \
       IDF_TOOLS_PATH=~/.espressif/tools \
       IDF_PYTHON_ENV_PATH=~/.espressif/tools/python/v6.0.1/venv
. ~/.espressif/v6.0.1/esp-idf/export.sh
export PATH="$HOME/.espressif/tools/ninja/1.12.1:$PATH"
```

The board enumerates as `/dev/cu.usbmodem1101` (USB-serial JTAG, 115200 baud).

## Provisioning

The firmware refuses to run without a provisioned seed. Provisioning is two
steps: `gen_seed.py` mints a 32-byte **master seed**, then `derive_keys.py`
derives the two root secrets (`d_0`, `sk_0`) from it and writes the NVS partition
CSV, which is built into an NVS image and flashed to the `nvs` partition as part
of `idf.py flash` (`nvs_create_partition_image(...)` in `main/CMakeLists.txt`).
Do this **once** before the first flash:

```bash
python3 scripts/gen_seed.py                  # 32-byte master seed (CSPRNG) → seed.key
python3 scripts/gen_seed.py -p "my passphrase"   # deterministic, TESTING ONLY
python3 scripts/derive_keys.py               # seed.key → seed.csv + seed.keys
```

With no arguments both scripts read and write at the **repo root** (where the
build and host tools expect them), so the two-command flow above just works from
anywhere. `derive_keys.py` writes two files from the seed: `seed.csv` (the NVS
image the build flashes) and `seed.keys` (a plaintext `d_0=`/`sk_0=` file that
`fetch_reports.py` reads to reconstruct the tag's private keys). All three files
hold **the tag's root secret in plaintext** — `seed.key` is the one secret
everything else derives from. Keep them out of version control (all are
`.gitignore`d) and treat them as sensitive.

Re-flashing rewrites the whole `nvs` partition, which wipes the writable
`esptag_st` namespace and **resets the persisted rotation counter to 0** (a
provisioning event). A plain reboot/reset does not.

## Build / Flash / Monitor

Requires the toolchain activated as above (`$IDF_PATH` must be set).

```bash
idf.py set-target esp32s3        # only when (re)configuring the target
idf.py build                     # configure + compile
idf.py -p /dev/cu.usbmodem1101 flash
idf.py -p /dev/cu.usbmodem1101 monitor          # Ctrl-] to exit
idf.py -p /dev/cu.usbmodem1101 flash monitor
idf.py fullclean                 # wipe build/
```

After editing `sdkconfig.defaults`, regenerate the active config —
defaults apply only when `sdkconfig` does not yet exist:

```bash
rm sdkconfig && idf.py reconfigure
```

`CONFIG_IDF_TARGET` lives in `sdkconfig.defaults`, so the regenerated config keeps
the esp32s3 target without re-running `set-target`.

### Capturing serial output non-interactively

`idf.py monitor` needs a TTY. To grab a fixed window of boot/advertising logs from
a script, pulse a reset over RTS and read the port with the venv's pyserial:

```bash
~/.espressif/tools/python/v6.0.1/venv/bin/python - <<'PY'
import serial, time
s = serial.Serial('/dev/cu.usbmodem1101', 115200, timeout=1)
s.setDTR(False); s.setRTS(True); time.sleep(0.1); s.setRTS(False)  # reset pulse
end = time.time() + 8
while time.time() < end:
    line = s.readline()
    if line: print(line.decode('utf-8', 'replace').rstrip())
s.close()
PY
```

## Verifying the broadcast over the air

`scripts/scan_findmy.py` is the receiver-side counterpart: it uses the
[FindMy.py](https://github.com/malmeloo/FindMy.py) library to scan for the tag's
Apple "offline finding" advertisement and reconstruct the full 28-byte `p_curr`
from the BLE address + payload, so you can match it against the `p_curr` the
firmware logs at each rotation. FindMy.py drives Bleak, so the usual BLE
permissions apply (on macOS the running terminal needs Bluetooth access).

```bash
python3 -m venv scripts/.venv && scripts/.venv/bin/pip install findmy   # once
scripts/.venv/bin/python scripts/scan_findmy.py            # scan 30 s
scripts/.venv/bin/python scripts/scan_findmy.py -d 0       # until Ctrl-C
scripts/.venv/bin/python scripts/scan_findmy.py --rssi -70 # widen the range
```

By default it shows only close advertisements (RSSI ≥ -40 dBm). Each epoch the tag
rotates its BLE address *and* key together (the address is derived from
`p_curr[0..5]`), so a rotation appears as a brand-new (address, key) entry rather
than the same address changing key — the script flags each newly-seen key as a
"new epoch (rotation)". The tag advertises a 25-byte "Separated" frame, so its
full key is recoverable; pair this with a short `ESPTAG_ROTATE_INTERVAL_MS` to
watch rotation live.

## Configuration

Project options live under **`menuconfig` → "esptag configuration"**
(`main/Kconfig.projbuild`). Kconfig defaults apply only when `sdkconfig` is
(re)generated — an existing `sdkconfig` keeps its current values.

| Option | Default | Purpose |
|---|---|---|
| `ESPTAG_ROTATE_INTERVAL_MS` | `900000` (15 min) | Epoch length: how often the key/identifier rotates. Drop to a few seconds to watch rotation while testing. |
| `ESPTAG_PERSIST_COUNTER` | `y` | Persist the rotation counter across reboots and fast-forward the ratchet to it at boot. Without it the tag always resumes at counter 0 and replays the same `p_0, p_1, …` sequence after every power cycle — a linkability regression. Unset = ephemeral test mode (no flash wear, reproducible from-zero). |
| `ESPTAG_ZEROIZE` | `y` | Scrub all intermediate key material (`mbedtls_platform_zeroize`) on every path, including error paths. Defines the `ZEROIZE` macro for `main/`. Disable only for local debugging where you want to inspect secrets left on the stack. |
| `ESPTAG_OWN_LOG_LEVEL` | `INFO` | Runtime log level applied at startup to this project's own tags (`main`, `crypto`, `nvs_store`, `tag`, `ble_adv`). |

**Logging.** The system-wide default is WARN (`CONFIG_LOG_DEFAULT_LEVEL_WARN`),
keeping IDF / NimBLE / controller components quiet; VERBOSE is compiled in
(`CONFIG_LOG_MAXIMUM_LEVEL_VERBOSE`) so it can be enabled at runtime. `app_main`
raises this project's own tags to `ESPTAG_OWN_LOG_LEVEL`. Add new modules to the
`OWN_LOG_TAGS` array in `main.c` to keep them at that level. The 2nd-stage
bootloader has its own pre-`app_main` level (also WARN).

**BLE stack.** NimBLE, configured broadcaster-only in `sdkconfig.defaults` (no
central/observer/peripheral, no GATT, no security/SM/RPA; `CONFIG_BT_NIMBLE_HS_PVCY=n`
so the firmware owns address rotation). See the commented BLE section in
`sdkconfig.defaults` for why each role is disabled.

## Layout

```
main/                firmware (crypto, tag, nvs_store, ble_adv, main)
  Kconfig.projbuild  the "esptag configuration" menu
components/micro_ecc vendored micro-ecc (secp224r1, compressed points)
scripts/gen_seed.py  generate the 32-byte master seed (seed.key)
scripts/derive_keys.py  derive d_0/sk_0 from the seed → NVS image (seed.csv)
scripts/scan_findmy.py  host-side BLE scanner; recovers p_curr over the air
sdkconfig.defaults   target, partition, log, and BLE configuration
DEVELOPING.md        developer guide (architecture, NimBLE, Find My, internals)
```
