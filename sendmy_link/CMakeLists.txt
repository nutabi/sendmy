# sendmy_link — the link layer of the sendmy protocol: packages a 28-byte key
# into a valid Apple offline-finding (Find My-style) BLE advertisement and puts
# it on the air. Knows nothing about what the key means.
#   bt             — NimBLE host + controller
#   esp_hw_support — esp_random (status byte)
# Returns esp_err_t (the platform convention for a standalone component), so it
# carries no dependency on any consumer's internal conventions.
idf_component_register(
    SRCS "sendmy_link.c"
    INCLUDE_DIRS "."
    REQUIRES bt esp_hw_support
)
