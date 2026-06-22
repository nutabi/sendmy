idf_component_register(
        SRCS "uECC.c"
        INCLUDE_DIRS "."
)

target_compile_definitions(${COMPONENT_LIB} PRIVATE
        uECC_SUPPORTS_secp224r1=1
        uECC_SUPPORT_COMPRESSED_POINT=1
        uECC_OPTIMIZATION_LEVEL=2
)

# The project builds at the IDF default -Og (CONFIG_COMPILER_OPTIMIZATION_DEBUG),
# and micro-ecc has no Xtensa assembly backend, so the portable C bignum loops
# (muladd / uECC_vli_mult) are the point-multiplication hot path and get no real
# optimization. Override to -O2 for this component only (the last -O flag on the
# command line wins); the rest of the firmware stays debug-friendly.
target_compile_options(${COMPONENT_LIB} PRIVATE -O2)