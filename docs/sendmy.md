# sendmy

sendmy is a project that explores using Apple's *Find My* offline-finding network as a covert uplink channel. It is built on top of an ESP32 (specifically the Seeed XIAO ESP32-S3) running ESP-IDF 6.0.

The repo has two reusable ESP-IDF components at its root (`sendmy_carrier` and `sendmy_link`), plus two standalone firmware examples (`espsend` and `esptag`) that use them.

---

## How the Find My network works

Every Apple device passively listens for BLE advertisements in the Apple offline-finding format. When it spots one, it grabs the 28-byte advertising key from the payload, hashes it with SHA-256, and uploads that hash alongside its own GPS coordinates to Apple's servers. The idea is that the owner of a lost device can query Apple with their private key and recover where it was last seen — without the relay devices ever learning whose key they forwarded.

The format itself is a BLE manufacturer-specific record (type `0xFF`, company `0x004C`) with a specific inner structure:

```
1e ff 4c 00   <- AD header + Apple company ID
12            <- OF type (offline finding = 0x12)
19            <- payload length = 25
00            <- status byte
<22 bytes>    <- key[6..27]
<1 byte>      <- key[0] high two bits
<1 byte>      <- hint (zero)
```

The BLE random address carries `key[5:0]` in reversed byte order, with the two MSBs forced to `1` (static random address). Between the address and the advertisement payload you can reconstruct the full 28 bytes.

Any firmware that can emit this exact frame, with a valid-looking 28-byte key, will be forwarded by nearby iPhones.

---

## The two components

### sendmy_link

`sendmy_link` is the BLE layer. It wraps NimBLE into a minimal non-connectable broadcaster. The public API is just two functions:

```c
esp_err_t sm_ll_init(void (*on_ready)(void), uint32_t adv_interval_ms);
esp_err_t sm_ll_set_key(const uint8_t key[28]);
```

`sm_ll_init` starts the NimBLE host task and calls `on_ready` once the host has synced with the controller. `sm_ll_set_key` can be called from any FreeRTOS task — it copies the key under a NimBLE mutex and posts an event to the host task's event queue, which then stops the current advertisement, sets the new random address derived from the key, and restarts. The address derivation is `key[5:0]` reversed with `| 0xC0` on the MSB.

`sendmy_link` has no knowledge of what the 28-byte key means — it just broadcasts it. Both `espsend` and `esptag` use it.

### sendmy_carrier

`sendmy_carrier` is the protocol layer on top of the link. It takes a 32-byte shared secret (the *Unilink ID*), a 32-bit message ID, and a single payload octet, and produces a 28-byte carrier:

```c
esp_err_t sm_cr_build_carrier(const uint8_t uid[32],
                              uint32_t      mid,
                              uint8_t       payload,
                              uint8_t       carrier[28]);
```

The first 27 bytes (the *Carrier ID*) are derived via HKDF-Expand:

```
info   = "smv1" || mid_be32
CID    = HMAC-SHA256(key=uid, data=info || 0x01)[:27]
carrier = CID || payload_byte
```

This is just the first T(1) block of RFC 5869 HKDF-Expand, with `uid` as the PRK. The final byte of the carrier is the payload octet, appended directly.

Because CID is a pseudorandom function of `(uid, mid)`, the carrier looks like a random 28-byte string to anyone without `uid`. Two carriers for the same `mid` but different payloads differ only in the last byte; two carriers for different `mid` values look completely unrelated.

---

## How the channel works

A receiver that knows `uid` can recover a transmitted byte for any given `mid` by querying Apple's servers:

1. Compute `CID = HKDF-Expand(uid, "smv1" || mid_be32, 27)`.
2. For each of the 256 possible payload values, form `carrier = CID || pl` and compute `SHA256(carrier)`.
3. Query Apple's location endpoint with these 256 hashes. The one that comes back with a result is the transmitted byte.

Apple stores reports for 7 days, so there is no need to be listening in real time. The throughput is limited by how often Apple's relay network files a report — practically one octet per minute is a reasonable rate, giving roughly 0.13 bits per second.

Only `espsend` uses `sendmy_carrier`. `esptag` uses `sendmy_link` directly with a P-224 key ratchet, and is a standard Find My tag rather than a data channel.

---

## Target hardware

Both examples target the Seeed XIAO ESP32-S3 (esp32s3, 8 MB flash). The `sdkconfig.defaults` in each example disables the BLE central/peripheral roles, trims the stack down to broadcaster-only, and routes crypto through mbedTLS.

## Repo layout

```
sendmy_carrier/     reusable component: carrier derivation (HKDF, PSA Crypto)
sendmy_link/        reusable component: NimBLE OF broadcaster
examples/
  espsend/          end-to-end data channel demo (uses both components)
  esptag/           Find My tag with key ratchet (uses sendmy_link only)
docs/               this directory
```
