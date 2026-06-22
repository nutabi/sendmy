idf_component_register(
    SRC_DIRS "."
    INCLUDE_DIRS "."
    REQUIRES micro_ecc mbedtls esp_hw_support nvs_flash bt sendmy_link i2c_bus bme680
)

# Secret-scrubbing (mbedtls_platform_zeroize of all intermediate key material on
# every path) is gated directly behind CONFIG_ESPTAG_ZEROIZE (menuconfig,
# default y) via #ifdef in the sources; see CLAUDE.md "ZEROIZE build flag".

# Build an NVS image from the provisioned seed and flash it to the `nvs`
# partition as part of `idf.py flash`. Generate seed.csv once beforehand from a
# master seed:
#   python3 scripts/gen_seed.py        # 32-byte master seed -> seed.key
#   python3 scripts/derive_keys.py     # seed.key -> seed.csv + seed.keys
# (both default to the repo root, so no path args are needed)
nvs_create_partition_image(nvs "${PROJECT_DIR}/seed.csv" FLASH_IN_PROJECT)
