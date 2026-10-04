"""Exit successfully when Docker reports the AMD container runtime."""

import json
import sys


runtime_info = json.load(sys.stdin)
if "amd" not in runtime_info:
    raise SystemExit("AMD runtime is missing")
