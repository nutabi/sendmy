idf_component_register(
    SRCS "espbench.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash sendmy_link sendmy_carrier
)
