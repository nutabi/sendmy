#!/usr/bin/env python3
"""Reads uid.hex from project root and writes nvs_data.csv for nvs_partition_gen.py."""

import sys
from pathlib import Path

uid_hex = Path(sys.argv[1]).read_text().strip()
assert len(uid_hex) == 64, "uid.hex must contain exactly 64 hex characters (32 bytes)"

out = Path(sys.argv[2])
out.write_text(
    "key,type,encoding,value\n"
    "sendmy,namespace,,\n"
    f"uid,data,hex2bin,{uid_hex}\n"
)
