"""流水输入解析：支持 JSON 与旧系统导出风格的 CSV。

JSON 形式（动作数组，字段见 Action）::

    [{"seq": 1, "action_id": "A1", "op": "receive", ...}]

CSV 表头（中英文均可，未知列忽略）::

    seq,action_id,op,material,warehouse,batch,age_start,qty,location,from_location,isolate_id,ref_doc
"""

from __future__ import annotations

import csv
import json

from .model import Action

# CSV 表头别名 -> 标准字段
_ALIASES = {
    "seq": "seq", "序号": "seq",
    "action_id": "action_id", "动作id": "action_id", "动作编号": "action_id", "id": "action_id",
    "op": "op", "动作": "op", "动作类型": "op", "类型": "op",
    "material": "material", "物料": "material", "物料编码": "material", "品号": "material",
    "warehouse": "warehouse", "仓": "warehouse", "仓库": "warehouse", "仓号": "warehouse",
    "batch": "batch", "批次": "batch", "批次号": "batch",
    "age_start": "age_start", "货龄起点": "age_start", "收料日期": "age_start", "日期": "age_start",
    "qty": "qty", "数量": "qty", "整数数量": "qty",
    "location": "location", "库位": "location", "目标库位": "location", "to_location": "location",
    "from_location": "from_location", "来源库位": "from_location",
    "isolate_id": "isolate_id", "隔离动作": "isolate_id", "隔离id": "isolate_id",
    "ref_doc": "ref_doc", "领料单号": "ref_doc", "引用单据": "ref_doc", "单据号": "ref_doc",
}

_OP_ALIASES = {
    "receive": "receive", "收料": "receive", "入库": "receive",
    "transfer": "transfer", "移位": "transfer", "挪库位": "transfer",
    "isolate": "isolate", "隔离": "isolate", "移入隔离": "isolate",
    "release": "release", "放行": "release", "解除隔离": "release",
    "issue": "issue", "领料": "issue", "领用": "issue",
    "return": "return", "退料": "return", "退回": "return",
}

_STR_FIELDS = ("action_id", "op", "material", "warehouse", "batch",
               "age_start", "location", "from_location", "isolate_id", "ref_doc")


class InputError(Exception):
    """输入文件本身的结构性错误（无法归到某个物料断点）。"""


def _coerce(row: dict, source: str, lineno: int) -> Action:
    data = {}
    for raw_key, value in row.items():
        if raw_key is None:  # DictReader 对超额列给出 None 键
            continue
        norm = raw_key.strip()
        key = _ALIASES.get(norm) or _ALIASES.get(norm.lower())
        if key is None:
            continue
        if value is None:
            continue
        value = value.strip() if isinstance(value, str) else value
        if value == "":
            continue
        data[key] = value

    try:
        seq = int(data.pop("seq"))
    except (KeyError, TypeError, ValueError):
        raise InputError(f"{source} 第 {lineno} 行缺少合法的 seq 序号")
    action_id = str(data.pop("action_id", f"line-{lineno}"))
    op_raw = str(data.pop("op", "")).strip().lower()
    op = _OP_ALIASES.get(op_raw)
    if op is None:
        raise InputError(f"{source} 第 {lineno} 行动作类型 {op_raw!r} 无法识别")

    material = data.pop("material", None)
    warehouse = data.pop("warehouse", None)
    if not material or not warehouse:
        raise InputError(f"{source} 第 {lineno} 行缺少物料或仓库")

    qty = data.pop("qty", None)
    if qty is not None:
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise InputError(f"{source} 第 {lineno} 行数量不是整数: {qty!r}")
        if qty <= 0:
            raise InputError(f"{source} 第 {lineno} 行数量必须为正整数")

    clean = {k: (str(v) if k in _STR_FIELDS and v is not None else v)
             for k, v in data.items()}
    return Action(seq=seq, action_id=action_id, op=op,
                  material=str(material), warehouse=str(warehouse),
                  qty=qty, raw=dict(row), **clean)


def parse_file(path: str) -> list[Action]:
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, list):
            raise InputError("JSON 顶层必须是动作数组")
        return [_coerce({k: ("" if v is None else v) for k, v in row.items()},
                        path, i + 1) for i, row in enumerate(payload)]

    if path.lower().endswith(".csv"):
        out: list[Action] = []
        # utf-8-sig 兼容带 BOM 的旧系统导出
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader, start=2):  # 表头占第 1 行
                out.append(_coerce(row, path, i))
        return out

    raise InputError(f"不支持的文件后缀（请使用 .json 或 .csv）: {path}")
