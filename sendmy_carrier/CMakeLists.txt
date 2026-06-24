idf_component_register(
    SRCS "sendmy_carrier.c" "p224.c"
    INCLUDE_DIRS "."
    REQUIRES mbedtls
)
