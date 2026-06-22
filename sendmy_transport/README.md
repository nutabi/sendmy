# sendmy_transport

The **transport layer** of the [sendmy](../README.md) protocol. It encodes **one
octet of application data per carrier**: a 28-octet value that the
[`sendmy_link`](../sendmy_link/README.md) layer broadcasts into Apple's Find My
(offline finding) network as a "key", such that a receiver holding a shared
secret can recover the octet by asking Apple whether specific carriers exist.

The medium is one-way and store-and-forward: a sender beacons a carrier, finder
devices nearby report its location to Apple, and the receiver later queries Apple
for the existence of that carrier. No data ever flows back to the sender.

## Threat model in one line

The secrecy and entropy of the **Unilink ID** is the entire security of the
scheme. Anyone who learns it can derive every carrier and read the channel;
anyone without it sees only opaque, key-like beacons indistinguishable from
ordinary Find My traffic.

## Glossary

| Term | Symbol | Size | Meaning |
|------|--------|------|---------|
| Unilink ID | `uid` | 32 octets | Pre-shared secret; the HKDF pseudorandom key (PRK). A "unilink" is the sender+receiver pair sharing it. |
| Message ID | `mid` | 4 octets | Zero-indexed, big-endian sequence number. One `mid` carries one octet. |
| Carrier ID | `cid` | 27 octets | Per-(uid, mid) value, independently derivable by sender and receiver. |
| Payload | — | 1 octet | The data octet, `0x00`–`0xFF`. |
| Carrier | — | 28 octets | `cid ‖ payload`; broadcast as a Find My key. |
| INFO | — | 4 octets | Domain-separation tag `73 6D 76 31` (`"smv1"`). |

## Carrier format

```
 0                                                            26 27     28
|---------------------------------------------------------------|---------|
| carrier_id (cid)                                              | payload |
|---------------------------------------------------------------|---------|
                       27 octets                                  1 octet
```

The first 27 octets are the Carrier ID; the 28th is the payload. The whole
28-octet carrier is what the link layer advertises as the offline-finding public
key.

### Carrier ID derivation

```
cid = HKDF-Expand(PRK = uid, info = INFO ‖ mid_be32, L = 27)
```

- **HKDF-Expand** is RFC 5869 with HMAC-SHA-256. Because `L = 27 ≤ 32`, this is a
  single block: `cid = HMAC-SHA-256(uid, INFO ‖ mid_be32 ‖ 0x01)[:27]`.
- **HKDF-Extract is omitted**: `uid` is used directly as the PRK, so it must
  already have ≥ 128 bits of entropy — from a CSPRNG, or a password-based KDF
  (Argon2/scrypt) over a weaker secret.
- `mid_be32` is the 4-octet big-endian Message ID.

Both endpoints derive the same `cid` offline; nothing about it is transmitted.

## Payload format

The transport carries an **octet stream**. There is no length field, checksum,
or framing inside a carrier — a carrier holds exactly one octet, and the Message
ID is the octet's index in the stream:

```
octet i of the stream  ⇄  payload of the carrier with mid = i
```

To send the byte string `B[0], B[1], …, B[n-1]`, the sender emits carriers for
`mid = 0 … n-1` with `payload = B[mid]`. The receiver walks the same `mid`
sequence and recovers one octet per query.

Properties and consequences:

- **One octet per carrier, one carrier per `mid`.** Throughput is bounded by how
  long each carrier must be advertised for Apple to observe and store a report
  (seconds in theory, often minutes in practice), so this is a low-rate channel.
- **The payload is unauthenticated.** The receiver infers the octet *solely* from
  which carrier exists; the value rides in the carrier's identity, not in any
  signed field. Integrity rests entirely on the fact that only a holder of `uid`
  could have produced a valid `cid`. There is no built-in detection of a flipped
  or injected octet beyond "a carrier with the right `cid` exists."
- **`mid` is a monotonic 32-bit counter and is not wrapped by the protocol.**
  Behaviour on exhaustion (≈ 4.29 billion octets) or `uid` rotation is
  implementation-defined.
- **Application framing is out of scope.** Any structure — message boundaries,
  length prefixes, error detection, encryption of the octet itself — is layered
  on top by the application.

### Reference test pattern

The reference sender (the espsend firmware) transmits a self-describing test
stream rather than real data: for each `mid`, the payload is

```
payload(mid) = 1 << (mid mod 8)     →  0x01, 0x02, 0x04, … 0x80, 0x01, …
```

a single walking one-hot bit that repeats every 8 ids, while `mid` advances
forever. This makes drops and off-by-one `mid` errors easy to spot end-to-end.

## Receiver operation

For a target `mid`, the receiver does not know the payload, so it enumerates all
256 possibilities:

1. Derive `cid = HKDF-Expand(uid, INFO ‖ mid_be32, 27)` once.
2. For each candidate `p` in `0x00 … 0xFF`, form `carrier = cid ‖ p` and compute
   its Find My lookup key `H = SHA-256(carrier)`.
3. Issue all 256 lookups to Apple in a single batch.
4. The payload is the `p` whose `H` returns a report. If none returns, the octet
   for that `mid` is **lost**.

Exactly one octet is recovered per batch (256 candidates → one Message ID). A
present report is sufficient; it is never decrypted (the receiver holds no
private key, and there is nothing to decrypt — the signal is existence).

### Loss and resynchronisation

A lost carrier advances the sender's `mid` without advancing the receiver's
recovered position. Receivers that fall behind must either accept the gap or
re-query a window of `mid` values. Resynchronisation policy is out of scope for
the transport and left to the application.

## Security considerations

- **`uid` secrecy and entropy are the whole game** (≥ 128 bits required). Treat it
  as a long-term shared key; provision it out of band.
- **Carriers are unlinkable without `uid`.** Each `(uid, mid)` produces a
  pseudorandom 27-octet `cid`, so on-air carriers look like unrelated Find My
  keys; an observer cannot tell two carriers came from the same unilink, recover
  `mid`, or read the payload.
- **No forward secrecy or replay protection** is provided by the transport. The
  same `(uid, mid)` always yields the same carrier; reusing a `mid` for a
  different octet leaks information and must be avoided (hence monotonic `mid`).
- **The payload octet is malleable in principle**: an adversary who learns a
  `cid` (e.g. by observing a beacon) could publish the sibling carrier for a
  different payload. Authenticate at the application layer if this matters.

## API

```c
#include "sendmy_transport.h"

esp_err_t sm_tl_build_carrier(const uint8_t uid[SM_TL_UID_LEN],   // 32-octet PRK
                              uint32_t      mid,                   // message id
                              uint8_t       payload,              // data octet
                              uint8_t       carrier[SM_TL_CARRIER_LEN]);  // out, 28
```

Builds `carrier = HKDF-Expand(uid, INFO ‖ mid_be32, 27) ‖ payload`. Returns
`ESP_OK`, or an `esp_err_t` on a PSA/crypto failure. Constants (`SM_TL_UID_LEN`,
`SM_TL_CID_LEN`, `SM_TL_CARRIER_LEN`, …) are defined in
[`sendmy_transport.h`](sendmy_transport.h). Carrier derivation uses the PSA
Crypto API (mbedTLS) for HMAC-SHA-256.

## Dependencies

`mbedtls` (PSA Crypto, for HMAC-SHA-256). Requires ESP-IDF >= 6.0.

## Logging

Logs under the tag **`sendmy_transport`** (`ESP_LOG*`). The component does not set
its own log level — the consuming application controls verbosity via
`esp_log_level_set("sendmy_transport", ...)`.
