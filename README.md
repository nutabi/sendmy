# sendmy

**sendmy** is a covert, one-way, store-and-forward channel that smuggles
application data through Apple's **Find My** (offline finding) network. A sender
beacons specially crafted BLE advertisements; nearby Apple devices ("finders")
report them to Apple as if they were lost-item trackers; a receiver holding a
shared secret later queries Apple to recover the data. Nothing flows back to the
sender, and to anyone without the secret the traffic is indistinguishable from
ordinary Find My beacons.

The protocol is split into two layers, shipped here as **two separate ESP-IDF
components** in one repository:

| Layer | Component | Role |
|-------|-----------|------|
| Transport | [`sendmy_transport`](sendmy_transport/README.md) | Derives a 28-octet *carrier* (`Carrier ID ‖ payload`) that encodes one octet of application data, keyed by a pre-shared **Unilink ID**. API prefix `sm_tl_`. |
| Link | [`sendmy_link`](sendmy_link/README.md) | Broadcasts a 28-byte key as a valid Find My BLE advertisement, owning the NimBLE stack, the manufacturer frame, and the rotating random address. API prefix `sm_ll_`. |

The layers meet at a clean interface: **28 bytes**. The transport produces a
28-octet carrier; the link layer treats those 28 bytes as an opaque key and puts
them on the air. The link layer has no knowledge of carriers, and the transport
has no knowledge of BLE — either can be used or tested on its own.

## How it fits together

```
uid (shared secret)
   │
   ├─ sender (this firmware):
   │     carrier = sm_tl_build_carrier(uid, mid, payload)   // transport: 28 octets
   │     sm_ll_set_key(carrier)                             // link: beacon as Find My key
   │                                                              │
   │                                                       Find My network
   │                                                              │
   └─ receiver (host-side):
         for p in 0..255: SHA-256(cid(uid, mid) ‖ p)        // query Apple
                          └─ a report exists ⇒ payload = p
```

One Message ID (`mid`) carries one octet; the receiver enumerates all 256
possible payloads for a `mid` and learns the octet from *which* carrier Apple has
seen. See [`sendmy_transport`](sendmy_transport/README.md) for the carrier
format, the HKDF derivation, the payload model, the receiver algorithm, and the
full threat model, and [`sendmy_link`](sendmy_link/README.md) for the on-air
frame format and threading model.

## Threat model in one line

The secrecy and entropy of the **Unilink ID** (≥ 128 bits) is the entire security
of the scheme: it derives every carrier. Without it, an observer sees only
opaque, key-like beacons and cannot link them, recover the message index, or read
the payload.

## Using the components

Each subdirectory is a standalone IDF component with its own `CMakeLists.txt` and
`idf_component.yml`. A consuming project depends on whichever layer(s) it needs;
because both live in one repository, a git dependency must point at the component
with `path:`. For example, in a consumer's `main/idf_component.yml`:

```yaml
dependencies:
  sendmy_transport:
    git: https://github.com/nutabi/sendmy.git
    path: sendmy_transport
  sendmy_link:
    git: https://github.com/nutabi/sendmy.git
    path: sendmy_link
```

and in `main/CMakeLists.txt`:

```cmake
idf_component_register(
    SRCS "app.c"
    REQUIRES sendmy_transport sendmy_link
)
```

A typical sender pulls in both; a receiver is host-side and uses neither (it only
needs the `uid` and the derivation, reimplemented off-device).

Both components require ESP-IDF >= 6.0. `sendmy_link` additionally requires `bt`
and `esp_hw_support`; `sendmy_transport` requires `mbedtls`.

## Disclaimer

sendmy is a research/educational exploration of the Find My medium. Operate it
only with devices and accounts you control, and review Apple's terms before use.
