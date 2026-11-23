"""召回影响分析 CLI。

用法::

    python3 -I run_slicer.py recall \\
        --flow samples/recall_flow.json \\
        --production samples/recall_production.json \\
        --request samples/recall_request_ab.json

成功：stdout 输出一份 JSON 清单（只列受影响的生产收料），退出码 0；
清单为空也算成功。失败：stderr 指出错误，退出码 1，且不输出清单。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import run_recall
from .model import RecallError


def _cmd_recall(args: argparse.Namespace) -> int:
    try:
        report = run_recall(args.flow, args.production, args.request)
    except RecallError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    json.dump(report.as_json(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="req_slicer recall",
        description="按外部到货召回请求计算受影响的生产收料及冻结区段")
    parser.add_argument("--flow", required=True,
                        help="仓内流水（.json 或 .csv，现有格式）")
    parser.add_argument("--production", required=True,
                        help="生产单清单 JSON")
    parser.add_argument("--request", required=True,
                        help="召回请求 JSON")
    parser.set_defaults(func=_cmd_recall)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
