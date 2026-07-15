idf_component_register(
    SRCS "espbench.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_driver_uart esp_driver_usb_serial_jtag vfs sendmy_link sendmy_carrier
)
