/* Copyright 2014, Kenneth MacKay. Licensed under the BSD 2-clause license. */

#ifndef _UECC_H_
#define _UECC_H_

#include <stdint.h>

/* This component is fixed to ESP32-S3 (Xtensa, 32-bit / word size 4); the
   upstream platform-selection machinery has been removed (see types.h). */

/* Optimization level; trade speed for code size.
   Larger values produce code that is faster but larger.
   Currently supported values are 0 - 4; 0 is unusably slow for most applications. */
#ifndef uECC_OPTIMIZATION_LEVEL
    #define uECC_OPTIMIZATION_LEVEL 2
#endif

/* This build supports only NIST P-224 (secp224r1); all other curves have been
   removed. */
#ifndef uECC_SUPPORTS_secp224r1
    #define uECC_SUPPORTS_secp224r1 1
#endif

/* Specifies whether compressed point format is supported.
   Set to 0 to disable point compression/decompression functions. */
#ifndef uECC_SUPPORT_COMPRESSED_POINT
    #define uECC_SUPPORT_COMPRESSED_POINT 1
#endif

struct uECC_Curve_t;
typedef const struct uECC_Curve_t * uECC_Curve;

#ifdef __cplusplus
extern "C"
{
#endif

#if uECC_SUPPORTS_secp224r1
uECC_Curve uECC_secp224r1(void);
#endif

/* uECC_RNG_Function type
The RNG function should fill 'size' random bytes into 'dest'. It should return 1 if
'dest' was filled with random data, or 0 if the random data could not be generated.
The filled-in values should be either truly random, or from a cryptographically-secure PRNG.

There is no predefined RNG on this embedded target; a correctly functioning RNG must be
registered with uECC_set_rng() before calling uECC_make_key(). */
typedef int (*uECC_RNG_Function)(uint8_t *dest, unsigned size);

/* Set the function that will be used to generate random bytes. Must be called (with a
   working RNG) before uECC_make_key(). */
void uECC_set_rng(uECC_RNG_Function rng_function);

/* uECC_make_key() - Create a public/private key pair.
    public_key  - Filled with the public key; for secp224r1 it is 56 bytes.
    private_key - Filled with the private key; for secp224r1 it is 28 bytes.
   Returns 1 on success, 0 on error. */
int uECC_make_key(uint8_t *public_key, uint8_t *private_key, uECC_Curve curve);

#if uECC_SUPPORT_COMPRESSED_POINT
/* uECC_compress() - Compress a public key. 'compressed' must be (curve size + 1) bytes;
   for secp224r1 that is 29 bytes. */
void uECC_compress(const uint8_t *public_key, uint8_t *compressed, uECC_Curve curve);
#endif /* uECC_SUPPORT_COMPRESSED_POINT */

/* uECC_valid_public_key() - Returns 1 if the public key is valid, 0 if invalid. */
int uECC_valid_public_key(const uint8_t *public_key, uECC_Curve curve);

/* uECC_compute_public_key() - Compute the public key for a private key.
   Returns 1 on success, 0 on error. */
int uECC_compute_public_key(const uint8_t *private_key, uint8_t *public_key, uECC_Curve curve);

#ifdef __cplusplus
} /* end of extern "C" */
#endif

#endif /* _UECC_H_ */
