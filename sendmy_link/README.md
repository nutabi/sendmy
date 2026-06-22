# sendmy_link

The **link layer** of the [sendmy](../README.md) protocol: a reusable ESP-IDF
component that broadcasts a 28-byte key as a valid Apple **offline-finding**
("Find My"-style) BLE advertisement. It frames one key and puts it on the air,
and knows nothing about *why* the key changes — rotating EC identities, arbitrary
data payloads, etc. are the caller's concern.

The 28-byte key is the link MTU. Anything that can produce 28-byte keys can drive
this component; in the sendmy protocol the producer is the
[`sendmy_transport`](../sendmy_transport/README.md) layer, but the link layer has
no dependency on it.

## Frame format

A Find My advertisement is 31 bytes (the legacy-advertising maximum) plus the
6-byte BLE random address. The 28-byte key is split across **both**:

| Field            | Bytes | Source                                  |
|------------------|-------|-----------------------------------------|
| BLE random addr  | 6     | `key[0..5]`, reversed, top 2 bits `0b11`|
| `0x1e 0xff 0x4c 0x00` | 4 | length + manufacturer-specific + Apple company id |
| `of_type` (`0x12`) | 1   | offline-finding type                    |
| `of_len` (`25`)  | 1     | length of remaining OF data             |
| `status`         | 1     | random                                  |
| `key_mid`        | 22    | `key[6..27]`                            |
| `key_hi`         | 1     | `key[0] >> 6` (recovers the 2 address bits clobbered by `0b11`) |
| `hint` (`0x00`)  | 1     |                                         |

Only the **28-byte key** traverses the Find My network — reports are indexed by
its hash. `of_type`/`of_len`/`status`/`hint` are framing a finder strips and
never forwards. A receiver reconstructs the key as:

```
key[0]     = (addr[5] & 0x3F) | (key_hi << 6)
key[1..5]  = addr[4..0]
key[6..27] = key_mid
```

The random address is forced to a *static random* address (top two bits `0b11`)
and rotates with the key. The component owns address rotation itself
(`CONFIG_BT_NIMBLE_HS_PVCY=n`); a static address would defeat the privacy design.

## API

```c
#include "sendmy_link.h"

esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms);
esp_err_t sm_ll_set_key(const uint8_t key[SM_LL_KEY_LEN /* 28 */]);
```

- **`sm_ll_init`** brings up the NimBLE port and spawns the host task. There is
  no shutdown path — `nimble_port_run` runs for the process lifetime. `on_ready`
  (optional, may be `NULL`) fires **once**, on the host task, when the stack
  first syncs; supply the initial key and arm any rotation timer there.
  `adv_interval_ms` pins `itvl_min == itvl_max` (one sweep of channels 37/38/39
  per window).
- **`sm_ll_set_key`** buffers the key and schedules it onto the NimBLE host task,
  which stops and restarts advertising under the new key/address on its next
  event-loop turn (i.e. by the next advertising cycle). A key set before sync is
  broadcast on sync. On a controller reset + re-sync the buffered key is
  re-advertised automatically. Safe to call from **any task**, and calls
  coalesce — only the most recently set key is applied.

`sm_ll_init` returns `ESP_OK` or the `nimble_port_init` error. `sm_ll_set_key`
returns `ESP_OK` once the key is buffered/queued, `ESP_ERR_INVALID_ARG` (NULL
key), or `ESP_ERR_INVALID_STATE` (called before init). Applying the key is
asynchronous, so a NimBLE GAP failure during apply is logged under the
`sendmy_link` tag rather than returned.

### Threading

`sm_ll_set_key` is safe to call from **any task**. It copies the key into a
mutex-protected buffer and posts a single apply request to the NimBLE host task
via the default event queue; the host task — the sole owner of the advertising
state — performs the GAP stop/restart. The caller never touches NimBLE GAP, and
the lock is held only around the 28-byte copy, never across a GAP call.

A key change is therefore not applied synchronously: it lands on the host task's
next event-loop turn, i.e. by the next advertising cycle. Between two cycles the
buffered key may be overwritten any number of times — only the most recent value
is ever broadcast (calls coalesce into one apply). `on_ready`, `on_sync`, and the
apply handler all run on the host task and so are mutually serialised.

## Usage

```c
static void on_ready(void) {
    sm_ll_set_key(current_key());   // publish the first key
    // ... arm any timer/task to call sm_ll_set_key() periodically; it no longer
    //     has to run on the host task ...
}

void app_main(void) {
    // ... bring up whatever produces keys ...
    ESP_ERROR_CHECK(sm_ll_init(on_ready, 2000 /* ms */));
    // sm_ll_set_key() may equally be called from here or any other task — a key
    // set before sync is broadcast on sync, so on_ready is optional.
}
```

## Dependencies

`bt` (NimBLE host + controller) and `esp_hw_support` (`esp_random` for the status
byte). Requires ESP-IDF >= 6.0.

## Logging

Logs under the tag **`sendmy_link`** (`ESP_LOG*`). The component does not set its
own log level — the consuming application controls verbosity via
`esp_log_level_set("sendmy_link", ...)`.
