# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.22)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espsend)

# -----------------
# NVS flash of UID
# -----------------
# Use the IDF build's Python interpreter rather than relying on the scripts'
# executable bit / shebang.
idf_build_get_property(PYTHON PYTHON)

# nvs_partition_gen.py moved from tools/ to components/ in IDF v6; try both.
set(NVS_TOOL $ENV{IDF_PATH}/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py)
if(NOT EXISTS ${NVS_TOOL})
    set(NVS_TOOL $ENV{IDF_PATH}/tools/nvs_flash/nvs_partition_generator/nvs_partition_gen.py)
endif()
if(NOT EXISTS ${NVS_TOOL})
    message(FATAL_ERROR "nvs_partition_gen.py not found under ${IDF_PATH}")
endif()

set(UID_HEX  ${CMAKE_SOURCE_DIR}/uid.hex)
set(NVS_CSV  ${CMAKE_BINARY_DIR}/nvs_data.csv)
set(NVS_BIN  ${CMAKE_BINARY_DIR}/nvs_data.bin)

if(NOT EXISTS ${UID_HEX})
    message(FATAL_ERROR "uid.hex not found at project root. "
            "Create it with 64 hex characters (32 bytes). ")
endif()

add_custom_command(
    OUTPUT  ${NVS_CSV}
    COMMAND ${PYTHON} ${CMAKE_SOURCE_DIR}/scripts/gen_nvs_csv.py
            ${UID_HEX} ${NVS_CSV}
    DEPENDS ${UID_HEX}
    COMMENT "Generating NVS CSV from uid.hex"
)

add_custom_command(
    OUTPUT  ${NVS_BIN}
    COMMAND ${PYTHON} ${NVS_TOOL} generate
            ${NVS_CSV} ${NVS_BIN} 0x6000
    DEPENDS ${NVS_CSV}
    COMMENT "Generating NVS partition binary"
)

add_custom_target(nvs_bin ALL DEPENDS ${NVS_BIN})

esptool_py_flash_to_partition(flash nvs ${NVS_BIN})
