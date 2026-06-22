# sendmy_transport — the transport layer of the sendmy protocol: derives a
# 28-octet carrier (Carrier ID || payload) that the link layer broadcasts as a
# Find My key.
#   mbedtls — PSA Crypto (HMAC-SHA-256) for the HKDF-Expand carrier derivation
idf_component_register(
    SRCS "sendmy_transport.c"
    INCLUDE_DIRS "."
    REQUIRES mbedtls
)
