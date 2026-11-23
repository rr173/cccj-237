"""离线仓储计费 CLI。

用法::

    python3 -I run_slicer.py billing \
        --flow samples/billing_flow.json \
        --prices samples/billing_prices.json \
        --start 3 --end 12

成功：stdout 输出一份 JSON 账单（可另用 --out 写文件），退出码 0；
账期内无占用时明细为空、总额为 0，也算成功。
失败：stderr 指出具体动作/库位与时段，退出码 1，不输出账单。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import run_billing
from .model import BillingError


def _cmd_billing(args: argparse.Namespace) -> int:
    try:
        report = run_billing(args.flow, args.prices, args.start, args.end)
    except BillingError as exc:
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
        prog="req_slicer billing",
        description="按账期离线核对仓内正常在库费用（按原收料与库位，不动库存）")
    parser.add_argument("--flow", required=True,
                        help="仓内流水（.json 动作数组或旧系统 .csv；动作带 minute）")
    parser.add_argument("--prices", required=True, help="JSON 价目表")
    parser.add_argument("--start", required=True, type=int,
                        help="账期起点分钟（含），非负整数")
    parser.add_argument("--end", required=True, type=int,
                        help="账期末端分钟（不含），整数且 end > start")
    parser.add_argument("--out", default=None,
                        help="可选：同时把 JSON 账单写入该文件")
    parser.set_defaults(func=_cmd_billing)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
