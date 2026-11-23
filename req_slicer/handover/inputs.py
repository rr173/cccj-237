"""交接对账输入解析与发布版本裁定。

交接文件 JSON::

    {
      "transfers": [
        {"doc_no": "T1",
         "issue": {"warehouse": "WH-A", "material": "RM", "action_seq": 3},
         "dest_warehouse": "WH-B"}
      ],
      "vouchers": [
        {"voucher_no": "V1", "publish_no": 3, "status": "signed",
         "doc_no": "T1", "ship_segment": [0, 5],
         "receipt": {"warehouse": "WH-C", "material": "RM",
                     "action_seq": 10, "start": 0}},
        {"voucher_no": "V2", "publish_no": 1, "status": "void"}
      ]
    }

- `transfers`：每票唯一 `doc_no`；`issue` 是发仓领料引用；`dest_warehouse`
  是承运商签收必须落到的指定收仓。同一领料不得出现在两票里。
- `vouchers`：`publish_no` 非负整数；`status` 为 `signed`（完整签收）或
  `void`（作废）。作废只带凭证号与发布号。
- 完整签收的收料区段末端由 `start + 发货区段长度` 推出；若超过该收料原始
  数量，引擎会拒绝（不把越界数据裁成合法签收）。

发布版本裁定（见 `resolve_versions`）：

1. 只考虑 `publish_no <= limit` 的记录；上限外的记录不参与裁定。
2. 同一 `voucher_no` 取其中最高 `publish_no` 的一条为准。
3. 同一 `voucher_no` + `publish_no` 在上限内出现多次：内容一致只算一次；
   内容不同直接报错。内容比较与文件行序无关。
4. 更低的发布号即使记录仍在文件里也不生效，因此用更早的上限可以重算历史。
"""

from __future__ import annotations

import json

from .model import HandoverError, Ref, Transfer

STATUS_ALIASES = {
    "signed": "signed", "签收": "signed", "完整签收": "signed",
    "void": "void", "作废": "void",
}


def _load_json(path: str, what: str) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise HandoverError(f"无法读取{what} {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise HandoverError(f"{what} {path} 不是合法 JSON: {exc}")


def _as_int(value, where: str, *, minimum: int | None = None) -> int:
    # bool 是 int 的子类，必须单独排除；浮点/字符串都不接受
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandoverError(
            f"{where} 必须是整数，实际为 {type(value).__name__}: {value!r}")
    if minimum is not None and value < minimum:
        raise HandoverError(f"{where} 不能小于 {minimum}：{value}")
    return value


def _as_str(value, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise HandoverError(
            f"{where} 必须是非空字符串，实际为 {value!r}")
    return value


def _check_fields(raw: dict, allowed: set[str], required: set[str],
                  where: str) -> None:
    if not isinstance(raw, dict):
        raise HandoverError(f"{where} 必须是对象，实际为 {type(raw).__name__}")
    extra = set(raw) - allowed
    if extra:
        raise HandoverError(f"{where} 含未知字段 {sorted(extra)}")
    missing = required - set(raw)
    if missing:
        raise HandoverError(f"{where} 缺少字段 {sorted(missing)}")


def _parse_ref(raw, where: str) -> Ref:
    _check_fields(raw, {"warehouse", "material", "action_seq"},
                  {"warehouse", "material", "action_seq"}, where)
    return Ref(warehouse=_as_str(raw["warehouse"], f"{where}.warehouse"),
               material=_as_str(raw["material"], f"{where}.material"),
               action_seq=_as_int(raw["action_seq"], f"{where}.action_seq",
                                 minimum=0))


def _parse_segment(raw, where: str) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise HandoverError(f"{where} 必须是 [起点, 末端) 两元素数组")
    start = _as_int(raw[0], f"{where}[0]", minimum=0)
    end = _as_int(raw[1], f"{where}[1]")
    if end <= start:
        raise HandoverError(f"{where} 必须满足 起点 < 末端，实际为 [{start}, {end})")
    return start, end


# ---------------------------------------------------------------------------
# 调拨清单
# ---------------------------------------------------------------------------
def parse_transfers(payload: dict) -> list[Transfer]:
    raw = payload.get("transfers")
    if not isinstance(raw, list) or not raw:
        raise HandoverError("交接文件的 transfers 必须是非空数组")
    transfers: list[Transfer] = []
    seen_docs: dict[str, int] = {}
    issue_owner: dict[Ref, str] = {}
    for i, item in enumerate(raw):
        where = f"transfers[{i}]"
        _check_fields(item, {"doc_no", "issue", "dest_warehouse"},
                      {"doc_no", "issue", "dest_warehouse"}, where)
        doc_no = _as_str(item["doc_no"], f"{where}.doc_no")
        if doc_no in seen_docs:
            raise HandoverError(
                f"调拨号 {doc_no!r} 重复（{where} 与 transfers[{seen_docs[doc_no]}]）")
        seen_docs[doc_no] = i
        issue = _parse_ref(item["issue"], f"{where}.issue")
        if issue in issue_owner:
            raise HandoverError(
                f"领料 {issue} 同时列在调拨 {issue_owner[issue]!r} "
                f"与 {doc_no!r} 里，同一领料不得列在两票")
        issue_owner[issue] = doc_no
        dest = _as_str(item["dest_warehouse"], f"{where}.dest_warehouse")
        if dest == issue.warehouse:
            raise HandoverError(
                f"调拨 {doc_no!r} 的指定收仓不能是发仓 {dest!r}")
        transfers.append(Transfer(doc_no=doc_no, issue=issue, dest_warehouse=dest))
    return transfers


# ---------------------------------------------------------------------------
# 凭证
# ---------------------------------------------------------------------------
def _voucher_content(raw: dict, where: str) -> tuple:
    """把一条凭证记录压成与行序无关的内容签名。"""
    _check_fields(raw, {"voucher_no", "publish_no", "status", "doc_no",
                        "ship_segment", "receipt"},
                  {"voucher_no", "publish_no", "status"}, where)
    status_raw = raw["status"]
    if not isinstance(status_raw, str) or status_raw not in STATUS_ALIASES:
        raise HandoverError(
            f"{where}.status 必须是 signed(签收) 或 void(作废)，实际为 {status_raw!r}")
    status = STATUS_ALIASES[status_raw]
    if status == "void":
        if "doc_no" in raw or "ship_segment" in raw or "receipt" in raw:
            raise HandoverError(f"{where} 是作废记录，不得再带认领内容")
        return ("void",)

    missing = {"doc_no", "ship_segment", "receipt"} - set(raw)
    if missing:
        raise HandoverError(f"{where} 完整签收缺少字段 {sorted(missing)}")
    doc_no = _as_str(raw["doc_no"], f"{where}.doc_no")
    start, end = _parse_segment(raw["ship_segment"], f"{where}.ship_segment")
    rec = raw["receipt"]
    _check_fields(rec, {"warehouse", "material", "action_seq", "start"},
                  {"warehouse", "material", "action_seq", "start"},
                  f"{where}.receipt")
    receipt = Ref(warehouse=_as_str(rec["warehouse"], f"{where}.receipt.warehouse"),
                  material=_as_str(rec["material"], f"{where}.receipt.material"),
                  action_seq=_as_int(rec["action_seq"],
                                     f"{where}.receipt.action_seq", minimum=0))
    rec_start = _as_int(rec["start"], f"{where}.receipt.start", minimum=0)
    # 收料区段末端 = start + 发货区段长度（等长、顺序不变）
    return ("signed", doc_no, start, end,
            receipt.warehouse, receipt.material, receipt.action_seq, rec_start,
            end - start)


def parse_vouchers(payload: dict) -> list[dict]:
    """解析凭证记录（保留原始记录位置，用于报错与去重）。"""
    raw = payload.get("vouchers", [])
    if not isinstance(raw, list):
        raise HandoverError("交接文件的 vouchers 必须是数组")
    records: list[dict] = []
    for i, item in enumerate(raw):
        where = f"vouchers[{i}]"
        content = _voucher_content(item, where)
        records.append({"voucher_no": _as_str(item["voucher_no"],
                                              f"{where}.voucher_no"),
                        "publish_no": _as_int(item["publish_no"],
                                              f"{where}.publish_no", minimum=0),
                        "where": where, "content": content})
    return records


def resolve_versions(records: list[dict], limit: int) -> list[dict]:
    """按发布号上限裁定每个凭证号的生效记录。

    返回生效记录（`content` 为 signed 的才认领货）；作废或没有记录的凭证号
    不出现在结果里。同号同发布号内容矛盾、或 signed 内容出现矛盾时报错。
    """
    _as_int(limit, "发布号上限", minimum=0)
    in_limit = [r for r in records if r["publish_no"] <= limit]
    by_version: dict[tuple[str, int], dict] = {}
    for rec in in_limit:
        key = (rec["voucher_no"], rec["publish_no"])
        prev = by_version.get(key)
        if prev is None:
            by_version[key] = rec
            continue
        if prev["content"] != rec["content"]:
            raise HandoverError(
                f"凭证 {rec['voucher_no']!r} 发布号 {rec['publish_no']} 在上限 "
                f"{limit} 内出现内容矛盾的记录：{prev['where']} 与 {rec['where']}")
        # 内容一致：只算一次，保留先出现者用于报错定位

    best: dict[str, dict] = {}
    for (voucher_no, publish_no), rec in by_version.items():
        cur = best.get(voucher_no)
        if cur is None or publish_no > cur["publish_no"]:
            best[voucher_no] = rec
    return [best[v] for v in sorted(best)]


# ---------------------------------------------------------------------------
# 顶层读取
# ---------------------------------------------------------------------------
def load_handover(path: str) -> tuple[list[Transfer], list[dict]]:
    payload = _load_json(path, "交接文件")
    if not isinstance(payload, dict):
        raise HandoverError("交接文件顶层必须是对象")
    extra = set(payload) - {"transfers", "vouchers"}
    if extra:
        raise HandoverError(f"交接文件含未知字段 {sorted(extra)}")
    transfers = parse_transfers(payload)
    records = parse_vouchers(payload)
    return transfers, records


def parse_limit(value) -> int:
    """发布号上限：非负整数（拒绝 bool/负数/浮点/非数字字符串）。"""
    if isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            raise HandoverError(f"发布号上限必须是非负整数，实际为 {value!r}")
        value = int(text)
    return _as_int(value, "发布号上限", minimum=0)
