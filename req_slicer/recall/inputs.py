"""召回分析输入解析与类型校验。

生产清单（JSON）::

    {"work_orders": [
      {"doc_no": "WO-1", "process": "ordered",
       "issues":   [{"warehouse": "WH", "material": "M", "action_seq": 5}],
       "receipts": [{"warehouse": "WH", "material": "N", "action_seq": 9}]}
    ]}

整份清单覆盖本次要追踪的全部生产：收料只要出现在任一生产单的 receipts
里就不是外部到货，未出现在任何 receipts 里的收料才是外部到货。

召回请求（JSON）::

    {"receipt": {"warehouse": "WH", "material": "M", "action_seq": 1},
     "segments": [[20, 40], [40, 60]]}

区段是原料段坐标系上的非空半开整数区间；相交或相邻的区段先合并再分析。
"""

from __future__ import annotations

import json
import os

from .model import RecallError, Ref, WorkOrder, normalize

PROCESS_ALIASES = {
    "ordered": "ordered", "保序": "ordered", "保序加工": "ordered",
    "mixed": "mixed", "混料": "mixed", "混料加工": "mixed",
}


def _load_json(path: str, what: str) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise RecallError(f"无法读取{what} {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise RecallError(f"{what} {path} 不是合法 JSON: {exc}")


def _as_int(value, where: str) -> int:
    # bool 是 int 的子类，必须单独排除；浮点/字符串都不接受
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecallError(f"{where} 必须是整数，实际为 {type(value).__name__}: {value!r}")
    return value


def _as_str(value, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecallError(f"{where} 必须是非空字符串，实际为 {value!r}")
    return value


def parse_ref(raw, where: str) -> Ref:
    if not isinstance(raw, dict):
        raise RecallError(f"{where} 必须是对象，实际为 {type(raw).__name__}")
    extra = set(raw) - {"warehouse", "material", "action_seq"}
    if extra:
        raise RecallError(f"{where} 含未知字段 {sorted(extra)}")
    for key in ("warehouse", "material", "action_seq"):
        if key not in raw:
            raise RecallError(f"{where} 缺少字段 {key}")
    return Ref(warehouse=_as_str(raw["warehouse"], f"{where}.warehouse"),
               material=_as_str(raw["material"], f"{where}.material"),
               action_seq=_as_int(raw["action_seq"], f"{where}.action_seq"))


def parse_production(path: str) -> list[WorkOrder]:
    payload = _load_json(path, "生产清单")
    if not isinstance(payload, dict) or "work_orders" not in payload:
        raise RecallError("生产清单顶层必须是含 work_orders 数组的对象")
    raw_orders = payload["work_orders"]
    if not isinstance(raw_orders, list):
        raise RecallError("生产清单 work_orders 必须是数组")
    if not raw_orders:
        raise RecallError("生产清单 work_orders 不能为空")

    orders: list[WorkOrder] = []
    seen_docs: set[str] = set()
    for i, raw in enumerate(raw_orders):
        where = f"work_orders[{i}]"
        if not isinstance(raw, dict):
            raise RecallError(f"{where} 必须是对象")
        extra = set(raw) - {"doc_no", "process", "issues", "receipts"}
        if extra:
            raise RecallError(f"{where} 含未知字段 {sorted(extra)}")
        doc_no = _as_str(raw.get("doc_no"), f"{where}.doc_no")
        if doc_no in seen_docs:
            raise RecallError(f"生产单号 {doc_no!r} 重复声明")
        seen_docs.add(doc_no)

        proc_raw = raw.get("process")
        if not isinstance(proc_raw, str) or proc_raw not in PROCESS_ALIASES:
            raise RecallError(
                f"{where}.process 必须是 ordered(保序) 或 mixed(混料)，实际为 {proc_raw!r}")
        process = PROCESS_ALIASES[proc_raw]

        for kind in ("issues", "receipts"):
            if kind not in raw:
                raise RecallError(f"{where} 缺少字段 {kind}")
            if not isinstance(raw[kind], list) or not raw[kind]:
                raise RecallError(f"{where}.{kind} 必须是非空数组")
        issues = [parse_ref(r, f"{where}.issues[{j}]")
                  for j, r in enumerate(raw["issues"])]
        receipts = [parse_ref(r, f"{where}.receipts[{j}]")
                    for j, r in enumerate(raw["receipts"])]
        orders.append(WorkOrder(doc_no=doc_no, process=process,
                                issues=issues, receipts=receipts))

    # 同一引用不得重复：跨生产单、同一生产单内部都算
    issue_owner: dict[Ref, str] = {}
    receipt_refs: dict[Ref, str] = {}
    for wo in orders:
        for ref in wo.issues:
            if ref in issue_owner:
                raise RecallError(
                    f"领料 {ref} 同时被生产单 {issue_owner[ref]!r} 与 {wo.doc_no!r} 引用"
                    if issue_owner[ref] != wo.doc_no else
                    f"领料 {ref} 在生产单 {wo.doc_no!r} 内重复引用")
            issue_owner[ref] = wo.doc_no
        for ref in wo.receipts:
            if ref in receipt_refs:
                raise RecallError(
                    f"收料 {ref} 同时被生产单 {receipt_refs[ref]!r} 与 {wo.doc_no!r} 声明为产出"
                    if receipt_refs[ref] != wo.doc_no else
                    f"收料 {ref} 在生产单 {wo.doc_no!r} 内重复声明")
            receipt_refs[ref] = wo.doc_no

    # 同一笔动作既作领料又作收料：两种坐标含义不同，拒绝歧义
    both = sorted(set(issue_owner) & set(receipt_refs), key=lambda r: r.sort_key())
    if both:
        ref = both[0]
        raise RecallError(
            f"引用 {ref} 既被领料引用又被收料引用，"
            f"无法同时作为两种身份定位（领料单 {issue_owner[ref]!r}，"
            f"产出单 {receipt_refs[ref]!r}）")
    return orders


def parse_recall_request(path: str) -> tuple[Ref, list[tuple[int, int]]]:
    payload = _load_json(path, "召回请求")
    if not isinstance(payload, dict):
        raise RecallError("召回请求顶层必须是对象")
    extra = set(payload) - {"receipt", "segments"}
    if extra:
        raise RecallError(f"召回请求含未知字段 {sorted(extra)}")
    if "receipt" not in payload:
        raise RecallError("召回请求缺少 receipt（召回的外部到货）")
    receipt = parse_ref(payload["receipt"], "receipt")

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise RecallError("召回请求 segments 必须是非空数组")
    segments: list[tuple[int, int]] = []
    for i, seg in enumerate(raw_segments):
        where = f"segments[{i}]"
        if not isinstance(seg, (list, tuple)) or len(seg) != 2:
            raise RecallError(f"{where} 必须是 [起点, 末端] 两元素数组")
        start = _as_int(seg[0], f"{where}[0]")
        end = _as_int(seg[1], f"{where}[1]")
        if start < 0:
            raise RecallError(f"{where} 起点不能为负：{start}")
        if end <= start:
            raise RecallError(f"{where} 必须满足 起点 < 末端，实际为 [{start}, {end})")
        segments.append((start, end))
    return receipt, normalize(segments)
