"""结果写出：领料交付表、期末切片（按状态/单据分开）、对账与断点报告。"""

from __future__ import annotations

import csv
import json
import os

from .model import NORMAL, ISOLATED, ISSUED
from .runner import RunResult

_DELIVERY_HEADER = [
    "line_id", "warehouse", "material", "doc_no", "issue_seq", "batch",
    "age_start", "receipt_seq", "seg_start", "seg_end", "qty",
    "returned_qty", "outstanding_qty",
]

_SLICE_HEADER = [
    "warehouse", "material", "state", "location", "holder", "batch",
    "age_start", "receipt_seq", "seg_start", "seg_end", "qty",
]

_RECON_HEADER = [
    "warehouse", "material", "received_total", "normal_total",
    "isolated_total", "issued_outstanding_total", "live_total",
    "delivery_total", "returned_total", "conserved", "coverage_ok",
]


def _write_csv(path: str, header: list[str], rows: list[list]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _slice_row(sl) -> list:
    return [sl.warehouse, sl.material, sl.state, sl.location or "",
            sl.holder or "", sl.batch, sl.age_start, sl.receipt_seq,
            sl.seg_start, sl.seg_end, sl.qty]


def write_outputs(run_result: RunResult, out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths: dict[str, str] = {}

    # 1) 领料交付表：每笔领料实际取自哪些收料批次/原料段，含原始货龄与退回数
    delivery_rows = []
    for gr in sorted(run_result.groups, key=lambda g: (g.warehouse, g.material)):
        for line in gr.ledger.lines:
            delivery_rows.append([
                line.line_id, line.warehouse, line.material, line.doc_no,
                line.issue_seq, line.batch, line.age_start, line.receipt_seq,
                line.seg_start, line.seg_end, line.qty,
                line.returned_qty, line.outstanding_qty,
            ])
    paths["delivery"] = os.path.join(out_dir, "delivery.csv")
    _write_csv(paths["delivery"], _DELIVERY_HEADER, delivery_rows)

    # 2) 期末未消费切片：按状态分开；隔离按隔离动作、占用按领料单据分列
    on_hand, isolated, issued, all_rows = [], [], [], []
    for gr in sorted(run_result.groups, key=lambda g: (g.warehouse, g.material)):
        for sl in gr.ledger.live_slices():
            row = _slice_row(sl)
            all_rows.append(row)
            if sl.state == NORMAL:
                on_hand.append(row)
            elif sl.state == ISOLATED:
                isolated.append(row)
            else:
                issued.append(row)

    paths["on_hand"] = os.path.join(out_dir, "period_end_on_hand.csv")
    paths["isolated"] = os.path.join(out_dir, "period_end_isolated.csv")
    paths["issued"] = os.path.join(out_dir, "period_end_issued.csv")
    paths["all_slices"] = os.path.join(out_dir, "period_end_slices.csv")
    _write_csv(paths["on_hand"], _SLICE_HEADER, on_hand)
    _write_csv(paths["isolated"], _SLICE_HEADER, isolated)
    _write_csv(paths["issued"], _SLICE_HEADER, issued)
    _write_csv(paths["all_slices"], _SLICE_HEADER, all_rows)

    # 3) 对账：数量守恒 + 原料段连续覆盖
    recon_rows = []
    recon_detail = []
    for gr in sorted(run_result.groups, key=lambda g: (g.warehouse, g.material)):
        rec = gr.ledger.reconciliation()
        recon_detail.append(rec)
        recon_rows.append([
            rec["warehouse"], rec["material"], rec["received_total"],
            rec["state_totals"][NORMAL], rec["state_totals"][ISOLATED],
            rec["state_totals"][ISSUED], rec["live_total"],
            rec["delivery_total"], rec["returned_total"],
            rec["conserved"], rec["coverage_ok"],
        ])
    paths["reconciliation"] = os.path.join(out_dir, "reconciliation.csv")
    _write_csv(paths["reconciliation"], _RECON_HEADER, recon_rows)

    # 4) 断点报告（无断点时 errors 为空数组）
    errors = []
    for gr in run_result.groups:
        if gr.error is not None:
            errors.append({
                "warehouse": gr.warehouse,
                "material": gr.material,
                "error": gr.error,
                "error_seq": gr.error_seq,
                "error_action_id": gr.error_action_id,
                "processed_before_break": gr.processed,
                "skipped_duplicates": gr.skipped_duplicates,
            })
    paths["errors"] = os.path.join(out_dir, "errors.json")
    with open(paths["errors"], "w", encoding="utf-8") as fh:
        json.dump({"errors": errors}, fh, ensure_ascii=False, indent=2)

    paths["reconciliation_json"] = os.path.join(out_dir, "reconciliation.json")
    with open(paths["reconciliation_json"], "w", encoding="utf-8") as fh:
        json.dump(recon_detail, fh, ensure_ascii=False, indent=2)
    return paths
