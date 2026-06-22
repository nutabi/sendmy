# Developer Guide

A deeper, concept-oriented companion to the [README](README.md). The README is
the *what* and *how to build*; this document is the *why* and the *background* —
the ESP-IDF / FreeRTOS execution model, the NimBLE stack, and the Apple "offline
finding" (Find My) format the firmware speaks. It's written for someone who has
to extend or debug this firmware and may not already know the platform.

If you only want to build, flash, and run, read the README. Come here when you
need to understand how the pieces fit, or when something in the BLE / crypto
path isn't behaving.

## Contents

1. [Mental model](#1-mental-model)
2. [The ESP32-S3 and ESP-IDF](#2-the-esp32-s3-and-esp-idf)
3. [The execution model: app_main, FreeRTOS, and the host task](#3-the-execution-model)
4. [The NimBLE stack](#4-the-nimble-stack)
5. [Apple offline finding (Find My) in detail](#5-apple-offline-finding-find-my-in-detail)
6. [The cryptographic core](#6-the-cryptographic-core)
7. [Module-by-module walkthrough](#7-module-by-module-walkthrough)
8. [Data flow: one boot, one rotation](#8-data-flow-one-boot-one-rotation)
9. [Storage: NVS layout](#9-storage-nvs-layout)
10. [Conventions to preserve](#10-conventions-to-preserve)
11. [Debugging playbook](#11-debugging-playbook)
12. [Where to look / extend](#12-where-to-look--extend)

---

## 1. Mental model

The tag is a **broadcast-only beacon**. It never connects to anything, never
listens, never holds a session. Every epoch (default 15 minutes) it:

1. ratchets a symmetric key forward one step,
2. derives a fresh elliptic-curve public key from it,
3. rewrites its BLE advertising payload and its BLE MAC address from that public
   key, and
4. keeps broadcasting until the next epoch.

A passive observer sees a string of unrelated-looking 31-byte advertisements
from a string of unrelated-looking random MAC addresses. Only the holder of the
two root secrets (`d_0`, `sk_0`) can regenerate the sequence and recognise that
all those advertisements came from one tag. That unlinkability *is* the product.

Everything in the firmware serves that loop. The crypto core produces the
rotating key; the BLE layer turns each key into an advertisement; NVS persists
the secrets and the epoch counter; `main` wires it together once at boot.

---

## 2. The ESP32-S3 and ESP-IDF

**ESP32-S3** is a dual-core Xtensa LX7 microcontroller with an integrated 2.4 GHz
radio that does both Wi-Fi and Bluetooth LE. We use only the BLE radio. Relevant
hardware facts for this project:

- **Hardware RNG.** `esp_fill_random` / `esp_random` draw from a true hardware
  entropy source (gated correctly only when the radio is on, which it is once
  NimBLE is up). This feeds micro-ecc's RNG hook and the per-advertisement
  `status` byte. There is no software CSPRNG to seed or reseed.
- **mbedTLS + PSA crypto.** ESP-IDF ships mbedTLS as a component, including the
  PSA Crypto API (`psa/crypto.h`). We use PSA only for SHA-256
  (`psa_hash_compute`) and the legacy `mbedtls_mpi` bignum API for modular
  scalar arithmetic. See the include-order comment at the top of `crypto.c`: on
  mbedTLS 4.x the legacy MPI API is behind a "private identifiers" macro, so
  `mbedtls/bignum.h` must be included before `psa/crypto.h`.
- **NVS (non-volatile storage).** A key/value store living in a flash partition,
  used here for the provisioned seed and the rotation counter. See §9.
- **USB-serial JTAG.** The S3 exposes a built-in USB CDC port (no external
  USB-UART chip), which is the `/dev/cu.usbmodem1101` you flash and monitor over.

**ESP-IDF** is Espressif's SDK: a CMake-based build system, a component model
(each `CMakeLists.txt` declares `REQUIRES`/`PRIV_REQUIRES` on other components),
FreeRTOS as the RTOS, and `idf.py` as the front-end for configure/build/flash/
monitor. `menuconfig` edits a `sdkconfig` file of `CONFIG_*` symbols that become
`#define`s in `sdkconfig.h`. This project's own knobs live in
`main/Kconfig.projbuild` under "esptag configuration" (see README → Configuration).

> ⚠️ **P-224 is not in mbedTLS 4.0** (which ships with IDF 6). That's why the
> curve math lives in the vendored micro-ecc instead of mbedTLS ECP. You cannot
> "just use mbedTLS" for the EC point multiply. This is recorded so nobody tries
> to delete the vendored library.

---

## 3. The execution model

ESP-IDF boots a 2nd-stage bootloader, then the IDF startup code, which finally
calls **`app_main`** on the "main" FreeRTOS task. `app_main` is *not* a loop —
when it returns, its task is cleaned up but the system keeps running because
other tasks exist. That shapes the whole design:

```
app_main (main task)                 NimBLE host task (spawned by ble_adv_init)
─────────────────────                ──────────────────────────────────────────
crypto_init
nvs_store_init
nvs_store_load_seed
nvs_store_load_counter
tag_init
ble_adv_init ───────────────────────► nimble_port_run()   (blocks forever)
return  (main task ends)                   │
                                           ├─ on_sync():  start advertising, arm timer
                                           ├─ on_rotate(): every epoch — rotate, re-advertise
                                           └─ ... runs for the life of the device
```

Two consequences a new developer must internalise:

1. **The `tag_t` must outlive `app_main`.** The host task keeps a pointer to it
   and reads/mutates it long after `app_main` returns. That's why `main.c` holds
   the tag as a **file-scope `static`**, not a stack local. Pass anything with
   shorter lifetime into `ble_adv_init` and you get a use-after-return.

2. **All tag mutation happens on the host task.** `on_sync` and the rotation
   callout both run on the NimBLE host task, so the tag is effectively
   single-threaded after init — *no locking is needed*, and none exists. If you
   ever touch `s_tag` from another task (a second timer, a console command), that
   invariant breaks and you'll need synchronisation. Don't, unless you mean to.

FreeRTOS primitives you'll meet: **tasks** (threads), and NimBLE's
**`ble_npl_callout`** — a one-shot timer that posts an event to a task's event
queue. We arm it for `ROTATE_INTERVAL_MS` and re-arm it at the end of each
`on_rotate`, giving a periodic epoch tick that always runs on the host task.

---

## 4. The NimBLE stack

**NimBLE** is the BLE stack ESP-IDF offers as an alternative to Bluedroid. It's
smaller and is the right choice for a broadcaster. Its architecture splits into:

- **Controller** — the link-layer / radio half, runs in its own task.
- **Host** — GAP/GATT/L2CAP/SM, the part our code calls into. Runs on the
  "host task" we spawn.
- **NPL (NimBLE Porting Layer)** — the OS-abstraction shim (`ble_npl_*`):
  callouts, event queues, mutexes, time. Lets the same host code run on FreeRTOS.

### How we drive it

`ble_adv_init` does the standard NimBLE bring-up:

```c
nimble_port_init();                 // init controller + host
ble_hs_cfg.reset_cb = on_reset;     // called if the controller drops sync
ble_hs_cfg.sync_cb  = on_sync;      // called when host & controller are ready
ble_npl_callout_init(&s_rotate_timer, nimble_port_get_dflt_eventq(), on_rotate, NULL);
nimble_port_freertos_init(host_task);   // spawn the host task → nimble_port_run()
```

You **cannot** start advertising from `ble_adv_init` directly — the host isn't
synced yet. NimBLE calls `on_sync` once it is; only there is it safe to set the
address and start advertising. If the controller resets, NimBLE calls `on_reset`
and then `on_sync` again, so `on_sync` is the single re-entrant "we're ready,
start broadcasting" hook. (That's why the callout is *init'd* in
`ble_adv_init` but *armed* in `on_sync` — a re-sync re-arms an existing timer
rather than re-initialising it.)

### Advertising specifics

We do **non-connectable, non-discoverable legacy advertising**:

```c
struct ble_gap_adv_params params = {
    .conn_mode = BLE_GAP_CONN_MODE_NON,   // nobody can connect
    .disc_mode = BLE_GAP_DISC_MODE_NON,   // no discoverable flags
};
ble_gap_adv_start(BLE_OWN_ADDR_RANDOM, NULL, BLE_HS_FOREVER, &params, NULL, NULL);
```

- **Legacy advertising** caps the payload at **31 bytes**. Our full
  advertisement is exactly 31 (see §5), which is deliberate — the Find My format
  fills the legacy AD to the brim.
- **The address must be changed while stopped.** You cannot reset the random
  address or the advertising data mid-advertisement, so `on_rotate` does
  `ble_gap_adv_stop()` → rotate → `adv_apply()` (set address, set data, start).
  A `BLE_HS_EALREADY` from the stop just means it wasn't running — not an error.
- **We own address rotation, not NimBLE.** `CONFIG_BT_NIMBLE_HS_PVCY=n` disables
  NimBLE's own resolvable-private-address machinery. A static, stack-managed
  address would defeat the privacy design — the whole point is that the MAC
  rotates *with* the key. So the firmware computes the address from `p_curr` on
  every epoch (`build_addr`) and sets it explicitly with `ble_hs_id_set_rnd`.
- **NimBLE return codes are not `status_t`.** They're `int`s compared against
  `BLE_HS_*` constants (`0` = success). This is one of the deliberate exceptions
  to the house `status_t` convention (§10).

The stack is configured **broadcaster-only** in `sdkconfig.defaults`: no central,
observer, peripheral connection, GATT, or security manager. The commented BLE
section there explains why each role is off — every disabled role is a smaller
attack surface and a smaller binary.

---

## 5. Apple offline finding (Find My) in detail

The tag impersonates an Apple "offline finding" accessory so that *any nearby
iPhone* will pick up the advertisement and relay an encrypted location report to
Apple's servers — that's how a Find My-style network gives a battery-powered,
network-less tag global coverage. We only implement the **broadcast** side; we
do not run Apple's report-decryption back end.

### The advertisement on the wire (31 bytes)

```
 byte:  0    1    2    3                      4 ............................. 30
       ┌────┬────┬────┬────┬──────────────────────────────────────────────────┐
       │ 1e │ ff │ 4c │ 00 │      27-byte offline-finding payload              │
       └────┴────┴────┴────┴──────────────────────────────────────────────────┘
        len  type  └─ Apple company id (0x004c, little-endian)
              │
              └─ 0xff = manufacturer-specific data
```

- `1e` = 30, the length of the single AD structure that follows.
- `ff` = AD type "manufacturer specific data".
- `4c 00` = company identifier `0x004C` (Apple), little-endian.
- The remaining 27 bytes are the `ble_adv_payload_t` (see `ble_adv.h`):

```
offset  field      bytes  value
  0     of_type      1     0x12   (offline-finding message type)
  1     of_len       1     0x19=25 (length of the remaining OF data)
  2     status       1     random  (battery/maintenance byte; we randomise it)
  3..24 key_mid     22     p_curr[6..27]   (middle of the public key)
 25     key_hi       1     p_curr[0] >> 6  (top 2 bits of the key's first byte)
 26     hint         1     0
```

### Where the public key goes

The 28-byte advertising key `p_curr` is split across **two** places in the
advertisement, which is the part that trips people up:

```
p_curr[0]      p_curr[1..5]        p_curr[6 ............ 27]
   │  └─ low 6 bits                        │
   │                                       └──────────────► payload.key_mid (22 bytes)
   └─ top 2 bits ──────────────────────────────────────────► payload.key_hi
   │
p_curr[0..5] (all 6 bytes) ──────────────────────────────► BLE random address
```

- **`p_curr[0..5]` → the BLE MAC address.** Apple's scheme puts the first 6 bytes
  of the key *in the advertiser's address*, not the payload. The address is set
  to those bytes **reversed to little-endian** (BLE addresses are transmitted
  LSB-first) with the **top two bits forced to `0b11`** to make it a valid
  *static random address*. That's `build_addr`:

  ```c
  for (int i = 0; i < 6; i++) addr[i] = tag->p_curr[5 - i];  // reverse
  addr[5] |= 0xC0;                                            // 0b11 prefix
  ```

- **Those forced top 2 bits would corrupt the key**, so the original top 2 bits
  of `p_curr[0]` are preserved separately in `key_hi = p_curr[0] >> 6`. A
  receiver reconstructs the full key as: address (reversed back, top 2 bits
  replaced by `key_hi`) ‖ `key_mid`.
- **`p_curr[6..27]` → `key_mid`.** The middle 22 bytes go straight into the
  payload.

So all 28 bytes of the key are recoverable by a scanner: 6 from the address, 22
from `key_mid`, and the 2 high bits from `key_hi`. Nothing is lost; the layout is
just dictated by Apple's format and the BLE static-random-address requirement.

> If you change the crypto to emit a different key length, this packing breaks.
> The 28-byte `P_LEN` is load-bearing for the 22 + 6 split. Keep them in sync.

---

## 6. The cryptographic core

All of this is in `crypto.c` / `crypto.h`, the only module that touches mbedTLS,
PSA, and micro-ecc. It is **stateless** — every function takes its inputs and
writes its outputs, holding no secrets between calls (the `tag` layer owns state).

### The KDF

A single SHA-256-based, NIST SP 800-108-style **counter-mode KDF**. For each
output block `i = 1, 2, …`:

```
block_i = SHA-256( z ‖ counter_i(BE32) ‖ info )
```

- `z` is the 32-byte input key (always `SK_LEN`).
- `counter_i` is a 4-byte **big-endian** block counter, sitting *between* `z` and
  `info`. It starts at 1 and increments per block.
- `info` is the label string (`"update"` or `"diversify"`), without its NUL.
- Output is the concatenation of blocks, truncated to the requested length.

This is `kdf()` in `crypto.c`. The two preconditions (`z_len == SK_LEN`,
`0 < info_len <= MAX_INFO_LEN`) are `assert`ed, not returned as errors — they are
programmer-error invariants fixed by the two call sites, not runtime-variable
conditions (see §10).

### The two key operations

**Ratchet** (`crypto_update_sk`): one KDF block.

```
sk_{i+1} = KDF(sk_i, "update", 32)
```

`crypto_advance_sk` just iterates this `counter` times to recompute `sk_i` from
`sk_0` — used at boot to fast-forward to the persisted epoch.

**Derive advertising key** (`crypto_derive_p`):

```
(u, v) = KDF(sk_i, "diversify", 72)      // 72 bytes = two 36-byte scalars
d_i    = d_0 · u + v   (mod n)           // mbedtls_mpi: compute_d()
P_i    = d_i · G                         // micro-ecc: uECC_compute_public_key
p_i    = compress(P_i) minus header byte // micro-ecc: uECC_compress, drop [0]
```

- `u` and `v` are 36 bytes each (wider than the 28-byte order so the
  reduce-mod-`n` is unbiased). `n` is the secp224r1 group order, the `P224_N`
  constant in `crypto.c`.
- The modular arithmetic uses `mbedtls_mpi` (bignum); the EC scalar multiply and
  point compression use micro-ecc (mbedTLS has no P-224).
- `uECC_compress` emits a 29-byte compressed point: 1 header byte (`0x02`/`0x03`,
  the y-parity) + 28-byte x-coordinate. We **strip the header**, so `p_i` is the
  28-byte x-coordinate. The y-parity is recoverable from the curve equation, and
  Apple's format doesn't carry it.
- `compute_d` deliberately **skips the `d_i == 0` check** (probability ~2⁻²²⁴);
  the guarded code is left commented for reference.

### Secret hygiene (ZEROIZE)

Every intermediate that touches secret material is scrubbed with
`mbedtls_platform_zeroize` on **every** path, including each error path, gated by
the `ZEROIZE` macro (driven by `CONFIG_ESPTAG_ZEROIZE`, default on). When reading
the code you'll see the pattern repeated, e.g. the `uv` buffer in
`crypto_derive_p` is scrubbed even on a late KDF failure because a partial
`(u, v)` plus any emitted `p_i` would leak `d_0`. If you add a function that
handles a secret, follow the same every-path discipline.

---

## 7. Module-by-module walkthrough

The dependency arrows point "downward"; nothing lower-level knows about anything
above it.

```
            main.c           (orchestration: wire everything once at boot)
           /   |   \
   ble_adv  nvs_store  tag    (ble_adv & main also use nvs_store / tag)
       \         |     /
        \        |    crypto  (tag → crypto)
         \       |   /
          (NimBLE) (NVS) (mbedTLS/PSA/micro-ecc)
```

| Module | Touches | Owns | Key functions |
|---|---|---|---|
| `crypto` | mbedTLS, PSA, micro-ecc | nothing (stateless) | `crypto_init`, `crypto_update_sk`, `crypto_advance_sk`, `crypto_derive_p` |
| `tag` | only `crypto` | the `tag_t` secret state | `tag_init`, `tag_rotate`, `tag_destroy` |
| `nvs_store` | NVS flash | nothing (reads/writes flash) | `nvs_store_init`, `nvs_store_load_seed`, `nvs_store_load_counter`, `nvs_store_save_counter` |
| `ble_adv` | NimBLE | the host task; retains the `tag_t*` | `ble_adv_init` (+ static `on_sync`/`on_rotate`/`adv_apply`/`build_payload`/`build_addr`) |
| `main` | — | the single file-scope `tag_t` | `app_main` |

**Why `tag` doesn't know about `nvs_store`.** The counter's persistence is
orchestrated by the layers *around* `tag`, not by `tag` itself: `main` loads the
counter and passes it to `tag_init`; `ble_adv` saves the new counter after each
`tag_rotate`. This keeps `tag` a pure crypto-state machine with no flash
dependency.

**`tag_t` is an exposed struct, not an opaque handle.** That's intentional so
`main` can give it static lifetime and `nvs_store_load_seed` can write into
`tag.d_0`/`tag.sk_0` directly. But the header is explicit that only `tag_*()`
functions may *mutate* the secret fields; other layers only populate the seed
before `tag_init` and read `p_curr`/`counter`.

---

## 8. Data flow: one boot, one rotation

### Boot

```
app_main:
  crypto_init()                         PSA up, secp224r1 selected, RNG hooked
  nvs_store_init()                      mount NVS
  nvs_store_load_seed(d_0, sk_0)        read root secrets → tag.d_0, tag.sk_0
                                        (aborts if unprovisioned)
  nvs_store_load_counter(&counter)      read persisted epoch (0 if never written)
  tag_init(&tag, counter):
      crypto_advance_sk(sk_0, counter)  fast-forward ratchet → sk_curr
      crypto_derive_p(d_0, sk_curr)     → p_curr  (the identity for this epoch)
  ble_adv_init(&tag)                    spawn host task; on_sync → advertise
```

After this, `app_main` returns and the host task owns the show.

### Each epoch (on the host task)

```
on_rotate timer fires:
  ble_gap_adv_stop()                    must be stopped to change addr/data
  tag_rotate(&tag):
      crypto_update_sk(sk_curr)         → sk_next   (ratchet one step)
      crypto_derive_p(d_0, sk_next)     → p_curr    (new identity)
      sk_curr = sk_next; counter++
  nvs_store_save_counter(counter)       persist new epoch (non-fatal if it fails)
  adv_apply():
      build_addr(tag, addr)             p_curr[0..5] reversed, top bits 0b11
      ble_hs_id_set_rnd(addr)           new random MAC
      build_payload(tag, &pl)           p_curr[6..27] + p_curr[0]>>6 + randoms
      ble_gap_adv_set_data(...)         1e ff 4c 00 + payload
      ble_gap_adv_start(...)            broadcast under the new identity
  ble_npl_callout_reset(...)            re-arm for the next epoch
```

**Failure handling is "keep broadcasting, never go dark."** If `tag_rotate`
fails, `on_rotate` re-advertises the *unchanged* identity and tries again next
epoch rather than stopping. If the counter persist fails, it's logged and
skipped — worst case is replaying from an earlier counter after a reboot, which
is recoverable, versus a dark tag, which isn't useful. This is the `ble_adv`
"log-and-continue" posture, distinct from `main`'s "abort on any failure."

---

## 9. Storage: NVS layout

Two namespaces in the same `nvs` partition:

| Namespace | Access | Keys | Written by |
|---|---|---|---|
| `esptag` | read-only at runtime | `d_0` (28 B blob), `sk_0` (32 B blob) | the flashed seed image (`seed.csv`), never the firmware |
| `esptag_st` | read-write | `counter` (u32) | `nvs_store_save_counter`, each epoch |

- The **seed** is baked into an NVS image at build time
  (`nvs_create_partition_image` in `main/CMakeLists.txt`, from `seed.csv`) and
  the firmware *never* writes it back. Missing namespace ⇒ "not provisioned" ⇒
  `nvs_store_load_seed` returns `STATUS_ERR` ⇒ `main` aborts. This is the safety
  rail against running with no/garbage key material.
- The **counter** lives in a *separate writable* namespace so the read-only seed
  and the mutable counter never share storage. A missing namespace **or** missing
  key reads back as `counter = 0` (first boot since provisioning), not an error —
  see the two `ESP_ERR_NVS_NOT_FOUND` branches in `nvs_store_load_counter`.
- **Re-flashing rewrites the whole `nvs` partition**, which wipes `esptag_st` and
  resets the persisted counter to 0 (a provisioning event). A plain reboot does
  not. So `idf.py flash` = "start the identity sequence over"; reset = "resume."
- Counter persistence is gated by `CONFIG_ESPTAG_PERSIST_COUNTER` (default on).
  With it **off**, `main` never loads and `ble_adv` never saves the counter, so
  the tag always boots at 0 and replays `p_0, p_1, …` — an ephemeral test mode
  (no flash wear, reproducible) that is a *linkability regression* you'd never
  ship. See README → Configuration.

---

## 10. Conventions to preserve

These are deliberate house rules, documented so you don't "fix" them:

- **`status_t` everywhere.** Every public function returns `STATUS_OK`/
  `STATUS_ERR` (`common.h`). The specific `esp_err_t`/PSA/NimBLE error is *logged*
  at the failure site (`ESP_LOGE`), not propagated. Callers treat non-OK as fatal
  (`main` aborts; `ble_adv` logs-and-continues). Don't introduce `esp_err_t`
  returns — the detail belongs in the log line. The two intentional exceptions:
  `crypto.c`'s `uecc_rng` follows micro-ecc's contract (`1` = success, the
  *inverse*) and stays a plain `int`; NimBLE codes stay `int` against `BLE_HS_*`.
- **`assert` for programmer-error preconditions** that cannot vary at runtime
  (the `kdf` length checks), `status_t` for everything that can fail at runtime.
- **Big-endian byte arrays** throughout; the KDF block counter is a 4-byte
  big-endian field between `z` and `info`. Size constants (`SK_LEN=32`,
  `P_LEN=D_LEN=N_LEN=28`, `HASH_LEN=32`) live in `crypto.h`.
- **ZEROIZE on every path**, including error paths, for any secret-touching
  buffer (§6).
- **The `tag_t` is single-threaded after init** and must have a lifetime that
  outlives `app_main` (§3). No locking; don't add a second writer.
- **`P_LEN == 28` is wired into the BLE packing** (the 6 + 22 + 2-bit split, §5).
  Changing the key length means reworking `ble_adv`.

---

## 11. Debugging playbook

**Reading logs.** The system default is WARN (quiet); this project's own tags
(`main`, `crypto`, `nvs_store`, `tag`, `ble_adv`) are raised to
`CONFIG_ESPTAG_OWN_LOG_LEVEL` (INFO by default) at the top of `app_main`. To see
more, bump that Kconfig level (VERBOSE is compiled in) or, for a one-off, the
`ESP_LOG_BUFFER_HEX_LEVEL` of the full advertisement in `adv_apply` is at DEBUG.

**Watching rotation without waiting 15 minutes.** Drop
`CONFIG_ESPTAG_ROTATE_INTERVAL_MS` to a few seconds (e.g. `5000`) via
`menuconfig` and reflash. Each `on_rotate` logs `counter=` and the new
`XX:XX:XX:XX:XX:XX` address (printed MSB-first, the order a scanner shows it).

**Capturing serial non-interactively.** `idf.py monitor` needs a TTY. Use the
pyserial reset-pulse recipe in the README (Capturing serial output) to grab a
fixed window from a script.

**Confirming the broadcast.** Scan with a phone app (nRF Connect, LightBlue) or
the `blew` BLE workbench. You should see a non-connectable advertiser whose MAC
and manufacturer data both change every interval. Match the MAC against the
`advertising as …` log line. The manufacturer data should start `4c 00 12 19`.

For a host-side check that goes one step further — recovering the full key, not
just eyeballing the MAC — run `scripts/scan_findmy.py`. It uses the
[FindMy.py](https://github.com/malmeloo/FindMy.py) library to parse the offline-
finding frame and reconstruct the 28-byte `p_curr` from the address + payload (the
reverse of the [§5 packing](#where-the-public-key-goes)), so you can diff it
against the `p_curr` the firmware logs each rotation. The `19` length byte routes
us to FindMy.py's "Separated" device type, which is the branch that exposes the
full key; the script defaults to RSSI ≥ -40 dBm (close devices only) and flags
key changes as rotations. Neighboring Apple kit occasionally trips a benign
`Invalid OF data length for NearbyOfflineFindingDevice` log from the library —
unrelated to our tag, and silenced unless you pass `-v`. Setup and usage are in
the [README → Verifying the broadcast](README.md#verifying-the-broadcast-over-the-air).

**Crypto correctness.** Don't eyeball it — cross-check `crypto_derive_p` against
an independent reference. `scripts/fetch_reports.py` derives the same epoch keys
on the host from `seed.keys` (one of the two files `derive_keys.py` writes), so a
mismatch with the firmware's logged `p_curr` points at a crypto bug.

**Common gotchas:**
- *Tag aborts at boot with "no provisioned seed"* — you didn't generate/flash
  `seed.csv` (README → Provisioning), or you flashed app-only without the NVS
  image.
- *Identifier sequence repeats after every power cycle* — `PERSIST_COUNTER` is
  off, or you keep reflashing (which resets the counter by design).
- *MPI / bignum API "undeclared"* — include-order regression; `mbedtls/bignum.h`
  must precede `psa/crypto.h` (the comment at the top of `crypto.c`).
- *Build can't find P-224* — someone tried to route EC math through mbedTLS;
  it's not there in 4.0, use micro-ecc.

---

## 12. Where to look / extend

| You want to… | Start in | Notes |
|---|---|---|
| Change the key-rotation scheme / KDF | `crypto.c` | Mirror the change in `scripts/fetch_reports.py` so host-side key derivation stays in sync. |
| Change the epoch length | `CONFIG_ESPTAG_ROTATE_INTERVAL_MS` | No code change; `ble_adv` reads it. |
| Change the advertised format | `ble_adv.c` + `ble_adv.h` | Mind the 31-byte legacy cap and the `P_LEN` split (§5). |
| Add persisted runtime state | `nvs_store.c` | Use the writable `esptag_st` namespace, not the read-only seed namespace. |
| Add a new log module | `main.c` `OWN_LOG_TAGS[]` | Keep it in sync with the module's `LOG_TAG`. |
| Verify the broadcast over the air | `scripts/scan_findmy.py` | FindMy.py BLE scanner; recovers `p_curr` to diff against the rotation logs. |
| Provision a new device | `scripts/gen_seed.py` → `seed.key`; `scripts/derive_keys.py` → `seed.csv` → flash | Re-flashing resets the rotation counter (§9). |

For build/flash/test mechanics and the toolchain-activation incantation, see the
[README](README.md). For the per-file rationale and the IDF-specific quirks of
this machine's toolchain, see [CLAUDE.md](CLAUDE.md).
