# Component `sendmy_carrier`

`sendmy_carrier` is the carrier layer of `sendmy`. It turns a one-byte message
into a 28-byte sequence that fits in `sendmy_link` payload.

## The format

A carrier is built from three inputs: a 32-byte shared secret (the *Unilink ID*,
`uid`), a 32-bit message ID (`mid`), and a single payload octet.

```
info    = "smv1" || mid_be32 || payload || attempt   ("smv1" is 0x73 0x6D 0x76 0x31)
block   = HMAC-SHA256(key=uid, info || 0x01)[:28]     (one HKDF-Expand T(1) block)
d       = int(block, big-endian)                      (attempt++ and redo until 1 <= d < n)
carrier = X(d * G)                                    (28-byte big-endian x-coordinate)
```

The HKDF input binds all three of `uid`, `mid`, and `payload` (the payload is
folded into `info`, never sent in the clear, and an observer cannot derive the
carrier for a different payload without `uid`). The derived block is interpreted
as a secp224r1 scalar `d`; if it falls outside `[1, n-1]` the one-byte `attempt`
counter is bumped and the block re-derived (rejection sampling — in practice
`attempt` is always `0`, since `P(reject) ~ 2^-112`). The carrier is the
big-endian x-coordinate of the public key `d * G`, so it is **always a valid
P-224 point** (see below) and a finder never silently drops it.

By querying 256 potential reports, the existence of a report implies the
payload attached.

## API

One function and the lengths it works with:

```c
#define SM_CR_UID_LEN     32
#define SM_CR_CARRIER_LEN 28

esp_err_t sm_cr_build_carrier(const uint8_t uid[32],
                              uint32_t      mid,
                              uint8_t       payload,
                              uint8_t       carrier[28]);
```

`sm_cr_build_carrier` writes the 28-byte carrier and returns `ESP_OK`, or an
error if the underlying crypto fails. The HMAC runs through the PSA Crypto API
(initialised lazily on first use), and every intermediate buffer is scrubbed
with `mbedtls_platform_zeroize` before the function returns, on success and
failure alike.

## Using it

`sendmy_carrier` depends only on `mbedtls` and targets ESP-IDF 6.0 or newer.

Either run:

```bash
idf.py add-dependency --git https://github.com/nutabi/sendmy --git-path sendmy_carrier sendmy_carrier
```

Or update `main/idf_component.yml` directly:

```yml
dependencies:
  sendmy_carrier:
    git: https://github.com/nutabi/sendmy
    path: sendmy_carrier
```

Then add `sendmy_carrier` to `REQUIRES` section inside `main/CMakeLists.txt`:

```cmake
idf_component_register(
    SRCS "your_app.c"
    REQUIRES sendmy_carrier
)
```

Feeding the result to `sendmy_link` is the whole point:

```c
uint8_t carrier[SM_CR_CARRIER_LEN];
sm_cr_build_carrier(uid, mid, payload, carrier);
sm_ll_set_key(carrier);
```

## Why the carrier is a valid P-224 point

A Find My advertising key is not an arbitrary 28-byte string — it is the
x-coordinate of a NIST P-224 (secp224r1) public key. A finder that hears the
advertisement reconstructs the point and uses it (ECDH) to encrypt its location
report. A raw HMAC-SHA256 truncation lands on a valid x-coordinate only about
half the time; for the rest no point exists on the curve, so a finder cannot
encrypt to it and files no report — **that message is silently lost**, with no
error on the sender side.

`sendmy_carrier` avoids this by deriving a scalar `d` from the HKDF inputs and
advertising `X(d * G)` (see "The format"), which is a valid point by
construction. Deliverability is therefore 100%, not ~50%.

The secp224r1 scalar multiply is hand-written in `p224.c`, because `mbedtls`/PSA
in ESP-IDF 6.0 no longer ships secp224r1. It uses fixed 7-word (224-bit) integer
arithmetic: a schoolbook multiply feeding the special NIST P-224 fast reduction
(`p = 2^224 - 2^96 + 1`, so no division), a Jacobian double-and-add, and a single
modular inverse at the end (~100 ms per carrier). The receiver
(`examples/espsend/scripts/fetch_reports.py`) derives the identical carrier with
the `cryptography` library, so the two ends stay in lockstep.
