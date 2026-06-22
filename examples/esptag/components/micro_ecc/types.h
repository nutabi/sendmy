/* Copyright 2015, Kenneth MacKay. Licensed under the BSD 2-clause license. */

#ifndef _UECC_TYPES_H_
#define _UECC_TYPES_H_

/* This component targets only ESP32-S3 (Xtensa, 32-bit). The upstream platform
   detection and the 8-bit/64-bit word-size variants have been removed; the word
   size is fixed at 4 bytes. */
#define uECC_WORD_SIZE 4

typedef int8_t wordcount_t;
typedef int16_t bitcount_t;
typedef int8_t cmpresult_t;

typedef uint32_t uECC_word_t;
typedef uint64_t uECC_dword_t;

#define HIGH_BIT_SET 0x80000000
#define uECC_WORD_BITS 32
#define uECC_WORD_BITS_SHIFT 5
#define uECC_WORD_BITS_MASK 0x01F

#endif /* _UECC_TYPES_H_ */
