"""交接对账 CLI。

用法::

    python3 -I run_slicer.py handover \\
        --flow samples/handover_flow.json \\
        --handover samples/handover_main.json \\
        --publish-limit 3

成功：stdout 输出一份 JSON 对账清单（可另用 --out 写文件），退出码 0；
清单为空也算成功。失败：stderr 指出具体记录/单据，退出码 1，不输出清单。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import run_handover
from .inputs import parse_limit
from .model import HandoverError


def _cmd_handover(args: argparse.Namespace) -> int:
    try:
        limit = parse_limit(args.publish_limit)
        report = run_handover(args.flow, args.handover, limit)
    except HandoverError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    text = json.dumps(report.as_json(), ensure_ascii=False, indent=2)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.write("\n")
        except OSError as exc:
            print(f"[错误] 无法写入 {args.out}: {exc}", file=sys.stderr)
            return 1
    sys.stdout.write(text + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="req_slicer handover",
        description="按发布号上限离线核对调拨发货的销账、催收与争议清单")
    parser.add_argument("--flow", required=True,
                        help="仓内流水（.json 或 .csv，现有格式）")
    parser.add_argument("--handover", required=True, help="交接文件 JSON")
    parser.add_argument("--publish-limit", required=True, type=int,
                        help="非负整数发布号上限；上限外记录不参与裁定")
    parser.add_argument("--out", default=None,
                        help="可选：同时把 JSON 清单写入该文件")
    parser.set_defaults(func=_cmd_handover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
