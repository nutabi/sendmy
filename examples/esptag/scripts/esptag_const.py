"""Shared esptag parameters for the host-side provisioning / verification scripts.

These constants must agree with the firmware (main/crypto.h, main/crypto.c, and
main/nvs_store.c). They are security-critical: a wrong curve order or key size
silently yields keys that do not match the device, with no error -- so they live
in one place rather than being copy-pasted into each script.

This module is stdlib-only and importable both before and after the venv
re-exec the findmy-based scripts perform (it sits next to them in scripts/, which
is on sys.path[0] whenever a script there is run directly).
"""

# Master seed length, in bytes. 32 = SHA-256 output, a full PRK.
SEED_LEN = 32

# Root-secret sizes -- mirror crypto.h (D_LEN, SK_LEN).
D_LEN = 28
SK_LEN = 32

# secp224r1 group order n, big-endian. Mirrors P224_N in crypto.c.
P224_N = int.from_bytes(
    bytes.fromhex("FFFFFFFFFFFFFFFFFFFFFFFFFFFF16A2E0B8F03E13DD29455C5C2A3D"),
    "big",
)

# NVS namespace / key names -- must match nvs_store.c.
NAMESPACE = "esptag"
KEY_D0 = "d_0"
KEY_SK0 = "sk_0"
