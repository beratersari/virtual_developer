#!/usr/bin/env python3
import sys
from pathlib import Path

src, dest = Path(sys.argv[1]), Path(sys.argv[2])
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))
print("copied", dest)
