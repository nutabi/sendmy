# Component `sendmy_carrier`

`sendmy_carrier` is the carrier layer of `sendmy`. It turns a one-byte message
into a 28-byte sequence that fits in `sendmy_link` payload.

## The format

A carrier is built from three inputs: a 32-byte shared secret (the *Unilink ID*,
`uid`), a 32-bit message ID (`mid`), and a single payload octet.

```
info    = "smv1" || mid_be32           ("smv1" is 0x73 0x6D 0x76 0x31)
CID     = HMAC-SHA256(key=uid, info || 0x01)[:27]
carrier = CID || payload
```

The first 27 bytes are the *Carrier ID* (CID). This is exactly the first `T(1)`
block of HKDF-Expand (RFC 5869) with `uid` as the pseudorandom key, truncated to
27 bytes. The 28th byte is the payload, appended verbatim.

By querying 256 potential reports, the existence of a report implies the
payload attached.

## API

One function and the lengths it works with:

```c
#define SM_CR_UID_LEN     32
#define SM_CR_CID_LEN     27
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
