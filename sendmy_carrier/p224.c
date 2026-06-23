#include "p224.h"

#include "esp_log.h"

#include <stddef.h>
#include <stdint.h>

static const char *TAG = "p224";

/*
 * ---------------------------------------------------------------------------
 * Pure 32-bit word arithmetic for secp224r1.
 *
 * The previous implementation drove every field operation through mbedtls's
 * bignum layer. On this target that meant a hardware-MPI interrupt round-trip
 * per multiply plus a software long-division per reduction -- thousands of each
 * per carrier, costing seconds. This version keeps everything in fixed 7-word
 * (224-bit) little-endian integers: a schoolbook multiply feeds the special
 * NIST P-224 fast reduction (p = 2^224 - 2^96 + 1 collapses to a few word
 * add/subs, no division), and the scalar multiply stays in Jacobian coordinates
 * so it needs a single modular inverse at the very end.
 *
 * Word-arithmetic techniques (comb multiply, fast reduction, binary-GCD
 * inverse) follow the well-known micro-ecc design.
 * ---------------------------------------------------------------------------
 */

#define P224_WORDS 7  ///< 224 bits / 32 = 7 little-endian words.

typedef uint32_t p224_word;   ///< Single 32-bit limb.
typedef uint64_t p224_dword;  ///< Double limb, for multiply accumulation.

#define P224_HIGH_BIT 0x80000000u

/* Curve constants as little-endian 7-word integers (word 0 = least significant).
 * Cross-checked against the verified big-endian byte constants in the spec. */

/** Field prime p = 2^224 - 2^96 + 1. */
static const p224_word P224_P[P224_WORDS] = {0x00000001, 0x00000000, 0x00000000, 0xFFFFFFFF,
                                             0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF};

/** Group order n (only used for the scalar range check). */
static const p224_word P224_N[P224_WORDS] = {0x5C5C2A3D, 0x13DD2945, 0xE0B8F03E, 0xFFFF16A2,
                                             0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF};

/** Generator x-coordinate Gx. */
static const p224_word P224_GX[P224_WORDS] = {0x115C1D21, 0x343280D6, 0x56C21122, 0x4A03C1D3,
                                              0x321390B9, 0x6BB4BF7F, 0xB70E0CBD};

/** Generator y-coordinate Gy. */
static const p224_word P224_GY[P224_WORDS] = {0x85007E34, 0x44D58199, 0x5A074764, 0xCD4375A0,
                                              0x4C22DFE6, 0xB5F723FB, 0xBD376388};

/*
 * ---------------------------------------------------------------------------
 * Very-long-integer (vli) primitives
 * ---------------------------------------------------------------------------
 */

static void vli_clear(p224_word *v)
{
    for (int i = 0; i < P224_WORDS; i++) {
        v[i] = 0;
    }
}

static void vli_set(p224_word *dst, const p224_word *src)
{
    for (int i = 0; i < P224_WORDS; i++) {
        dst[i] = src[i];
    }
}

static int vli_isZero(const p224_word *v)
{
    p224_word bits = 0;
    for (int i = 0; i < P224_WORDS; i++) {
        bits |= v[i];
    }
    return bits == 0;
}

/** Returns nonzero iff bit @p bit of @p v is set. */
static p224_word vli_testBit(const p224_word *v, int bit)
{
    return v[bit >> 5] & ((p224_word)1 << (bit & 31));
}

/** Returns the sign of (a - b): 1, 0, or -1. */
static int vli_cmp(const p224_word *a, const p224_word *b)
{
    for (int i = P224_WORDS - 1; i >= 0; i--) {
        if (a[i] > b[i]) {
            return 1;
        }
        if (a[i] < b[i]) {
            return -1;
        }
    }
    return 0;
}

/** r = a + b, returns the carry out (0 or 1). r may alias a or b. */
static p224_word vli_add(p224_word *r, const p224_word *a, const p224_word *b)
{
    p224_word carry = 0;
    for (int i = 0; i < P224_WORDS; i++) {
        p224_word sum = a[i] + b[i] + carry;
        // sum == a[i] only when b[i]+carry is 0 or wraps a full 2^32, in which
        // case the incoming carry is already the correct outgoing carry.
        if (sum != a[i]) {
            carry = (sum < a[i]);
        }
        r[i] = sum;
    }
    return carry;
}

/** r = a - b, returns the borrow out (0 or 1). r may alias a or b. */
static p224_word vli_sub(p224_word *r, const p224_word *a, const p224_word *b)
{
    p224_word borrow = 0;
    for (int i = 0; i < P224_WORDS; i++) {
        p224_word diff = a[i] - b[i] - borrow;
        if (diff != a[i]) {
            borrow = (diff > a[i]);
        }
        r[i] = diff;
    }
    return borrow;
}

/** v >>= 1 (in place). */
static void vli_rshift1(p224_word *v)
{
    p224_word carry = 0;
    for (int i = P224_WORDS - 1; i >= 0; i--) {
        p224_word t = v[i];
        v[i] = (t >> 1) | carry;
        carry = t << 31;
    }
}

/** (r0,r1,r2) += a*b, a 96-bit running accumulator for the comb multiply. */
static void vli_muladd(p224_word a, p224_word b, p224_word *r0, p224_word *r1, p224_word *r2)
{
    p224_dword prod = (p224_dword)a * b;
    p224_dword r01 = ((p224_dword)(*r1) << 32) | *r0;
    r01 += prod;
    *r2 += (r01 < prod);
    *r1 = (p224_word)(r01 >> 32);
    *r0 = (p224_word)r01;
}

/** result = a * b, a 2*P224_WORDS (14-word) product. */
static void vli_mult(p224_word *result, const p224_word *a, const p224_word *b)
{
    p224_word r0 = 0, r1 = 0, r2 = 0;
    for (int k = 0; k < P224_WORDS; k++) {
        for (int i = 0; i <= k; i++) {
            vli_muladd(a[i], b[k - i], &r0, &r1, &r2);
        }
        result[k] = r0;
        r0 = r1;
        r1 = r2;
        r2 = 0;
    }
    for (int k = P224_WORDS; k < 2 * P224_WORDS - 1; k++) {
        for (int i = k - (P224_WORDS - 1); i < P224_WORDS; i++) {
            vli_muladd(a[i], b[k - i], &r0, &r1, &r2);
        }
        result[k] = r0;
        r0 = r1;
        r1 = r2;
        r2 = 0;
    }
    result[2 * P224_WORDS - 1] = r0;
}

/*
 * ---------------------------------------------------------------------------
 * Field arithmetic modulo p
 * ---------------------------------------------------------------------------
 */

/**
 * @brief Fast reduction of a 14-word product modulo p = 2^224 - 2^96 + 1.
 *
 * Uses the NIST P-224 word recombination T = s1 + s2 + s3 - s4 - s5 (validated
 * exhaustively against a reference modulo), folding the signed high carry back
 * in via 2^224 == 2^96 - 1 (mod p), then a single conditional subtraction.
 */
static void fe_reduce(p224_word *result, const p224_word *c)
{
    // Per-lane signed combination of the product words.
    int64_t val[P224_WORDS];
    val[0] = (int64_t)c[0] - (int64_t)c[7] - (int64_t)c[11];
    val[1] = (int64_t)c[1] - (int64_t)c[8] - (int64_t)c[12];
    val[2] = (int64_t)c[2] - (int64_t)c[9] - (int64_t)c[13];
    val[3] = (int64_t)c[3] + (int64_t)c[7] + (int64_t)c[11] - (int64_t)c[10];
    val[4] = (int64_t)c[4] + (int64_t)c[8] + (int64_t)c[12] - (int64_t)c[11];
    val[5] = (int64_t)c[5] + (int64_t)c[9] + (int64_t)c[13] - (int64_t)c[12];
    val[6] = (int64_t)c[6] + (int64_t)c[10] - (int64_t)c[13];

    p224_word t[P224_WORDS];
    int64_t acc = 0;
    for (int k = 0; k < P224_WORDS; k++) {
        acc += val[k];
        t[k] = (p224_word)acc;
        acc >>= 32;  // arithmetic shift: signed carry/borrow out of lane k
    }

    // Fold the high part: value -= acc*p each pass (add acc at lane 3, subtract
    // acc at lane 0). Converges from either sign in a few passes.
    while (acc != 0) {
        int64_t top = acc;
        int64_t carry = 0;
        for (int k = 0; k < P224_WORDS; k++) {
            int64_t v = (int64_t)t[k];
            if (k == 0) {
                v -= top;
            } else if (k == 3) {
                v += top;
            }
            carry += v;
            t[k] = (p224_word)carry;
            carry >>= 32;
        }
        acc = carry;
    }

    // t is now in [0, 2^224) < 2p; one conditional subtraction finishes it.
    if (vli_cmp(t, P224_P) >= 0) {
        vli_sub(result, t, P224_P);
    } else {
        vli_set(result, t);
    }
}

/** r = a * b mod p. */
static void fe_mul(p224_word *r, const p224_word *a, const p224_word *b)
{
    p224_word product[2 * P224_WORDS];
    vli_mult(product, a, b);
    fe_reduce(r, product);
}

/** r = a^2 mod p. */
static void fe_sqr(p224_word *r, const p224_word *a)
{
    fe_mul(r, a, a);
}

/** r = a + b mod p. Assumes a, b in [0, p). */
static void fe_add(p224_word *r, const p224_word *a, const p224_word *b)
{
    p224_word carry = vli_add(r, a, b);
    if (carry || vli_cmp(r, P224_P) >= 0) {
        vli_sub(r, r, P224_P);
    }
}

/** r = a - b mod p. Assumes a, b in [0, p). */
static void fe_sub(p224_word *r, const p224_word *a, const p224_word *b)
{
    p224_word borrow = vli_sub(r, a, b);
    if (borrow) {
        vli_add(r, r, P224_P);
    }
}

/** uv = (uv + (uv odd ? mod : 0)) >> 1, the halving step of the binary inverse. */
static void fe_inv_update(p224_word *uv, const p224_word *mod)
{
    p224_word carry = 0;
    if (uv[0] & 1) {
        carry = vli_add(uv, uv, mod);
    }
    vli_rshift1(uv);
    if (carry) {
        uv[P224_WORDS - 1] |= P224_HIGH_BIT;
    }
}

/**
 * @brief r = input^{-1} mod p, via the binary extended GCD ("the Great Divide").
 *
 * Only called once per scalar multiply, to invert the final Jacobian Z.
 */
static void fe_inv(p224_word *r, const p224_word *input, const p224_word *mod)
{
    p224_word a[P224_WORDS], b[P224_WORDS], u[P224_WORDS], v[P224_WORDS];

    if (vli_isZero(input)) {
        vli_clear(r);
        return;
    }

    vli_set(a, input);
    vli_set(b, mod);
    vli_clear(u);
    u[0] = 1;
    vli_clear(v);

    int cmp;
    while ((cmp = vli_cmp(a, b)) != 0) {
        if (!(a[0] & 1)) {
            vli_rshift1(a);
            fe_inv_update(u, mod);
        } else if (!(b[0] & 1)) {
            vli_rshift1(b);
            fe_inv_update(v, mod);
        } else if (cmp > 0) {
            vli_sub(a, a, b);
            vli_rshift1(a);
            if (vli_cmp(u, v) < 0) {
                vli_add(u, u, mod);
            }
            vli_sub(u, u, v);
            fe_inv_update(u, mod);
        } else {
            vli_sub(b, b, a);
            vli_rshift1(b);
            if (vli_cmp(v, u) < 0) {
                vli_add(v, v, mod);
            }
            vli_sub(v, v, u);
            fe_inv_update(v, mod);
        }
    }
    vli_set(r, u);
}

/*
 * ---------------------------------------------------------------------------
 * Point arithmetic in Jacobian coordinates: (X, Y, Z) == affine (X/Z^2, Y/Z^3),
 * with Z == 0 denoting the point at infinity.
 * ---------------------------------------------------------------------------
 */

/** R = 2*R (in place). EFD "dbl-2001-b", valid because a = -3 on secp224r1. */
static void jac_double(p224_word *X, p224_word *Y, p224_word *Z)
{
    if (vli_isZero(Z)) {
        return;  // 2*O = O
    }

    p224_word delta[P224_WORDS], gamma[P224_WORDS], beta[P224_WORDS], alpha[P224_WORDS];
    p224_word t1[P224_WORDS], t2[P224_WORDS], X3[P224_WORDS], Y3[P224_WORDS], Z3[P224_WORDS];

    fe_sqr(delta, Z);          // delta = Z^2
    fe_sqr(gamma, Y);          // gamma = Y^2
    fe_mul(beta, X, gamma);    // beta  = X*gamma

    // alpha = 3*(X - delta)*(X + delta)
    fe_sub(t1, X, delta);
    fe_add(t2, X, delta);
    fe_mul(alpha, t1, t2);
    fe_add(t1, alpha, alpha);
    fe_add(alpha, t1, alpha);

    // X3 = alpha^2 - 8*beta   (Z3 computed first while Y, Z are intact)
    fe_add(t1, Y, Z);          // Z3 = (Y + Z)^2 - gamma - delta
    fe_sqr(Z3, t1);
    fe_sub(Z3, Z3, gamma);
    fe_sub(Z3, Z3, delta);

    fe_sqr(X3, alpha);
    fe_add(t2, beta, beta);
    fe_add(t2, t2, t2);
    fe_add(t2, t2, t2);        // t2 = 8*beta
    fe_sub(X3, X3, t2);

    // Y3 = alpha*(4*beta - X3) - 8*gamma^2
    fe_add(t1, beta, beta);
    fe_add(t1, t1, t1);        // t1 = 4*beta
    fe_sub(t1, t1, X3);
    fe_mul(Y3, alpha, t1);
    fe_sqr(t2, gamma);
    fe_add(t1, t2, t2);
    fe_add(t1, t1, t1);
    fe_add(t1, t1, t1);        // t1 = 8*gamma^2
    fe_sub(Y3, Y3, t1);

    vli_set(X, X3);
    vli_set(Y, Y3);
    vli_set(Z, Z3);
}

/** R = R + (ax, ay) (in place), R Jacobian and (ax, ay) affine. madd-2007-bl. */
static void jac_add_affine(p224_word *X, p224_word *Y, p224_word *Z, const p224_word *ax,
                           const p224_word *ay)
{
    if (vli_isZero(Z)) {
        // O + a = a (lift the affine point with Z = 1).
        vli_set(X, ax);
        vli_set(Y, ay);
        vli_clear(Z);
        Z[0] = 1;
        return;
    }

    p224_word Z1Z1[P224_WORDS], U2[P224_WORDS], S2[P224_WORDS], H[P224_WORDS], rr[P224_WORDS];
    p224_word HH[P224_WORDS], I[P224_WORDS], J[P224_WORDS], V[P224_WORDS];
    p224_word X3[P224_WORDS], Y3[P224_WORDS], Z3[P224_WORDS], t1[P224_WORDS];

    fe_sqr(Z1Z1, Z);           // Z1Z1 = Z^2
    fe_mul(U2, ax, Z1Z1);      // U2 = ax*Z1Z1
    fe_mul(S2, ay, Z);         // S2 = ay*Z*Z1Z1
    fe_mul(S2, S2, Z1Z1);
    fe_sub(H, U2, X);          // H = U2 - X
    fe_sub(rr, S2, Y);         // rr = 2*(S2 - Y)
    fe_add(rr, rr, rr);

    if (vli_isZero(H)) {
        // Equal affine x: rr == 0 => R == (ax, ay) (double); else R == -(ax, ay).
        if (vli_isZero(rr)) {
            jac_double(X, Y, Z);
        } else {
            vli_clear(Z);  // point at infinity
        }
        return;
    }

    fe_sqr(HH, H);             // HH = H^2
    fe_add(I, HH, HH);
    fe_add(I, I, I);           // I = 4*HH
    fe_mul(J, H, I);           // J = H*I
    fe_mul(V, X, I);           // V = X*I

    // X3 = rr^2 - J - 2*V
    fe_sqr(X3, rr);
    fe_sub(X3, X3, J);
    fe_add(t1, V, V);
    fe_sub(X3, X3, t1);

    // Y3 = rr*(V - X3) - 2*Y*J
    fe_sub(t1, V, X3);
    fe_mul(Y3, rr, t1);
    fe_mul(t1, Y, J);
    fe_add(t1, t1, t1);
    fe_sub(Y3, Y3, t1);

    // Z3 = (Z + H)^2 - Z1Z1 - HH
    fe_add(t1, Z, H);
    fe_sqr(Z3, t1);
    fe_sub(Z3, Z3, Z1Z1);
    fe_sub(Z3, Z3, HH);

    vli_set(X, X3);
    vli_set(Y, Y3);
    vli_set(Z, Z3);
}

/*
 * ---------------------------------------------------------------------------
 * Byte <-> word conversion (big-endian bytes, little-endian words)
 * ---------------------------------------------------------------------------
 */

static void bytes_to_native(p224_word *native, const uint8_t *bytes)
{
    vli_clear(native);
    for (int i = 0; i < P224_LEN; i++) {
        unsigned b = P224_LEN - 1 - i;
        native[b >> 2] |= (p224_word)bytes[i] << (8 * (b & 3));
    }
}

static void native_to_bytes(uint8_t *bytes, const p224_word *native)
{
    for (int i = 0; i < P224_LEN; i++) {
        unsigned b = P224_LEN - 1 - i;
        bytes[i] = (uint8_t)(native[b >> 2] >> (8 * (b & 3)));
    }
}

/**
 * @brief Scrub a buffer so the compiler cannot drop the writes as a dead store.
 *
 * The secret-bearing locals here are never read again after scrubbing, so a
 * plain memset would be a legal dead-store-elimination target at -O2 (CWE-14).
 * Writing through a volatile pointer makes each store an observable access that
 * must be preserved -- the same guarantee mbedtls_platform_zeroize gives, but
 * without pulling mbedtls into this otherwise self-contained file.
 */
static void secure_zero(void *buf, size_t len)
{
    volatile uint8_t *p = (volatile uint8_t *)buf;
    while (len-- > 0) {
        *p++ = 0;
    }
}

/*
 * ---------------------------------------------------------------------------
 * Public API implementation
 * ---------------------------------------------------------------------------
 */

bool p224_scalar_in_range(const uint8_t d[P224_LEN])
{
    p224_word v[P224_WORDS];
    bytes_to_native(v, d);
    // Accept iff 1 <= d < n.
    bool in_range = !vli_isZero(v) && vli_cmp(v, P224_N) < 0;
    secure_zero(v, sizeof(v));
    return in_range;
}

esp_err_t p224_base_mult_x(const uint8_t d[P224_LEN], uint8_t out_x[P224_LEN])
{
    p224_word scalar[P224_WORDS];
    p224_word Rx[P224_WORDS], Ry[P224_WORDS], Rz[P224_WORDS];
    p224_word zinv[P224_WORDS], zinv2[P224_WORDS], xaff[P224_WORDS];
    esp_err_t status = ESP_FAIL;

    bytes_to_native(scalar, d);
    vli_clear(Rx);
    vli_clear(Ry);
    vli_clear(Rz);  // R = O

    // Left-to-right double-and-add over the scalar bits, MSB -> LSB. The
    // accumulator stays in Jacobian coordinates, so neither step inverts.
    for (int i = P224_LEN * 8 - 1; i >= 0; i--) {
        jac_double(Rx, Ry, Rz);
        if (vli_testBit(scalar, i)) {
            jac_add_affine(Rx, Ry, Rz, P224_GX, P224_GY);
        }
    }

    if (vli_isZero(Rz)) {
        // Impossible for a scalar in [1, n-1] on this prime-order curve; defensive.
        ESP_LOGE(TAG, "d*G resolved to the point at infinity");
        goto cleanup;
    }

    // Convert back to affine: x = X / Z^2 mod p. This is the only inversion.
    fe_inv(zinv, Rz, P224_P);
    fe_sqr(zinv2, zinv);
    fe_mul(xaff, Rx, zinv2);
    native_to_bytes(out_x, xaff);
    status = ESP_OK;

cleanup:
    // Scrub every secret-derived intermediate (d is owned/scrubbed by the caller).
    secure_zero(scalar, sizeof(scalar));
    secure_zero(Rx, sizeof(Rx));
    secure_zero(Ry, sizeof(Ry));
    secure_zero(Rz, sizeof(Rz));
    secure_zero(zinv, sizeof(zinv));
    secure_zero(zinv2, sizeof(zinv2));
    secure_zero(xaff, sizeof(xaff));
    return status;
}
