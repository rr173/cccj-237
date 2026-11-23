#!/usr/bin/env python3
"""离线批处理启动器：在隔离模式(-I)下显式注入项目根目录。

用法（在项目根或任意目录均可）::

    python3 -I run_slicer.py run --input samples/flow_main.json --out-dir out/main
    python3 -I run_slicer.py recall --flow samples/recall_flow.json \
        --production samples/recall_production.json \
        --request samples/recall_request_ab.json
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from req_slicer.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
