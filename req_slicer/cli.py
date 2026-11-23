"""命令行入口：离线批处理，可重复跑，输出全部为普通 CSV/JSON 文件。

用法::

    python3 -I -m req_slicer.cli run --input 流水.json --out-dir 输出目录/
    python3 -I -m req_slicer.cli run --input 旧系统导出.csv --out-dir 输出目录/
    python3 -I -m req_slicer.cli recall --flow 流水.json \\
        --production 生产清单.json --request 召回请求.json
"""

from __future__ import annotations

import argparse
import sys

from .engine.inputs import parse_file, InputError
from .engine.model import NORMAL, ISOLATED, ISSUED
from .engine.outputs import write_outputs
from .engine.runner import run
from .recall.cli import _cmd_recall


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        actions = parse_file(args.input)
    except InputError as exc:
        print(f"[致命] 输入文件无法解析：{exc}", file=sys.stderr)
        return 2

    result = run(actions)
    paths = write_outputs(result, args.out_dir)

    print(f"输入动作 {len(actions)} 条，物料/仓分组 {len(result.groups)} 个")
    for gr in result.groups:
        status = "断点" if gr.error else "完成"
        print(f"  - {gr.warehouse}/{gr.material}: {status}，"
              f"执行动作 {gr.processed}，幂等跳过 {gr.skipped_duplicates}")
        rec = gr.ledger.reconciliation()
        totals = rec["state_totals"]
        print(f"      收料合计 {rec['received_total']} | 在库 {totals[NORMAL]} | "
              f"隔离 {totals[ISOLATED]} | 领料占用 {totals[ISSUED]} | "
              f"守恒={rec['conserved']} 段覆盖={rec['coverage_ok']}")
        if gr.error:
            print(f"      断点(seq={gr.error_seq}, action={gr.error_action_id}): {gr.error}")

    print("产物：")
    for name, path in paths.items():
        print(f"  {name:18s} -> {path}")
    return 1 if result.has_errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="req_slicer",
        description="把仓内动作流水解释成每笔领料的批次切片与期末未消费切片")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="执行一次离线批处理")
    p_run.add_argument("--input", required=True, help="流水文件（.json 或 .csv）")
    p_run.add_argument("--out-dir", required=True, help="产物输出目录")
    p_run.set_defaults(func=_cmd_run)

    p_recall = sub.add_parser(
        "recall", help="按外部到货召回请求计算受影响的生产收料与冻结区段")
    p_recall.add_argument("--flow", required=True,
                          help="仓内流水（.json 或 .csv，现有格式）")
    p_recall.add_argument("--production", required=True, help="生产单清单 JSON")
    p_recall.add_argument("--request", required=True, help="召回请求 JSON")
    p_recall.set_defaults(func=_cmd_recall)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
