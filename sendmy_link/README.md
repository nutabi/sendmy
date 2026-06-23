# Component `sendmy_link`

`sendmy_link` is the link layer of `sendmy`. It takes an arbitrary 28-byte
sequence and broadcasts it as a valid Offline Finding (OF) advertisement.

The component is a thin wrapper around NimBLE configured as a non-connectable,
non-discoverable broadcaster.

## The advertisement

Apple's OF format is a BLE manufacturer-specific record (AD type `0xFF`, company
`0x004C`). `sendmy_link` lays the 28-byte key across the random address and the
manufacturer payload exactly as a real tag does:

```
1e ff 4c 00   AD header + Apple company ID
12            OF type (offline finding)
19            payload length = 25
00            status byte
<22 bytes>    key[6..27]
<1 byte>      key[0] >> 6   (the two MSBs of key[0])
00            hint
```

The remaining six key bytes ride in the BLE random address: `key[5:0]` in
reversed byte order, with the top two bits of the last octet forced to `1` so
the controller treats it as a static random address. Between the address and the
payload, a receiver can reconstruct all 28 bytes.

## API

The whole surface is two functions and one constant (`SM_LL_KEY_LEN`, 28):

```c
esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms);
esp_err_t sm_ll_set_key(const uint8_t key[28]);
```

`sm_ll_init` brings up the NimBLE host task and returns immediately. Once the
host has synced with the controller it calls `on_ready` (once), which is your
cue that `sm_ll_set_key` will actually take effect. `adv_interval_ms` must be in
`[20, 10240]` (the spec's range in 0.625 ms units); anything else is rejected up
front with `ESP_ERR_INVALID_ARG`.

`sm_ll_set_key` is thread-safe. It copies the key under a NimBLE mutex and posts
an event to the host task, which stops the current advertisement, derives the
new random address, rebuilds the payload, and restarts. Calling it before
`sm_ll_init` returns `ESP_ERR_INVALID_STATE`. You can call it as often as you
like; each call rotates the advertised identity.

## Using it

`sendmy_link` depends only on `bt` and targets ESP-IDF 6.0 or newer. NimBLE must
be enabled in your build (`CONFIG_BT_ENABLED`, `CONFIG_BT_NIMBLE_ENABLED`).

Either run:

```bash
idf.py add-dependency \
    --git https://github.com/nutabi/sendmy \
    --git-path sendmy_link \
    sendmy_link
```

Or update `main/idf_component.yml` directly:

```yml
dependencies:
  sendmy_link:
    git: https://github.com/nutabi/sendmy
    path: sendmy_link
```

Then add `sendmy_link` to `REQUIRES` section inside `main/CMakeLists.txt`:

```cmake
idf_component_register(
    SRCS "your_app.c"
    REQUIRES sendmy_link
)
```

A minimal sender is then just:

```c
static void on_ready(void) {
    uint8_t key[SM_LL_KEY_LEN] = { /* 28 bytes */ };
    sm_ll_set_key(key);
}

void app_main(void) {
    sm_ll_init(on_ready, 1000);  // advertise every 1000 ms
}
```

See `espsend` for the carrier-driven version and `esptag` for one that rotates
the key on a timer.
