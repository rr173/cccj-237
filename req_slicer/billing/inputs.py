"""价目表 JSON 解析与校验。

价目表::

    {
      "threshold_minutes": 5,
      "rates": [
        {"warehouse": "WH1", "location": "A", "effective_minute": 0,
         "price_tier1": 10, "price_tier2": 18},
        {"warehouse": "WH1", "location": "A", "effective_minute": 8,
         "price_tier1": 12, "price_tier2": 20}
      ]
    }

- ``threshold_minutes``：统一的**正整数**门槛（分钟）；每分钟按该分钟开始时
  该原料段已累计的正常在库时长选档，达到门槛用第二档。
- ``rates``：按仓与库位给出的费率，``effective_minute`` 为非负整数生效分钟，
  同一 (仓, 库位) 内生效分钟不得重复；两档单价都是非负整数（**零价有效**）。
- 每分钟取当时最新生效（``effective_minute <= 分钟`` 的最后一条）的费率。
"""

from __future__ import annotations

import json

from .model import BillingError, PriceBook, RateEntry

_ALLOWED_RATE_FIELDS = {"warehouse", "location", "effective_minute",
                        "price_tier1", "price_tier2"}
_ALLOWED_TOP_FIELDS = {"threshold_minutes", "rates"}


def _load_json(path: str) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise BillingError(f"无法读取价目表 {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise BillingError(f"价目表 {path} 不是合法 JSON: {exc}")


def _as_int(value, where: str, *, minimum: int) -> int:
    # bool 是 int 的子类，必须单独排除；浮点/字符串都不接受
    if isinstance(value, bool) or not isinstance(value, int):
        raise BillingError(
            f"{where} 必须是整数，实际为 {type(value).__name__}: {value!r}")
    if value < minimum:
        raise BillingError(f"{where} 不能小于 {minimum}：{value}")
    return value


def _as_str(value, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise BillingError(f"{where} 必须是非空字符串，实际为 {value!r}")
    return value


def parse_price_book(path: str) -> PriceBook:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise BillingError("价目表顶层必须是对象")
    extra = set(payload) - _ALLOWED_TOP_FIELDS
    if extra:
        raise BillingError(f"价目表含未知字段 {sorted(extra)}")
    if "threshold_minutes" not in payload:
        raise BillingError("价目表缺少 threshold_minutes")
    threshold = _as_int(payload["threshold_minutes"],
                        "threshold_minutes", minimum=1)

    raw_rates = payload.get("rates")
    if not isinstance(raw_rates, list):
        raise BillingError("价目表 rates 必须是数组（可为空）")

    grouped: dict[tuple[str, str], list[RateEntry]] = {}
    seen: dict[tuple[str, str], set[int]] = {}
    for i, raw in enumerate(raw_rates):
        where = f"rates[{i}]"
        if not isinstance(raw, dict):
            raise BillingError(f"{where} 必须是对象")
        unknown = set(raw) - _ALLOWED_RATE_FIELDS
        if unknown:
            raise BillingError(f"{where} 含未知字段 {sorted(unknown)}")
        missing = _ALLOWED_RATE_FIELDS - set(raw)
        if missing:
            raise BillingError(f"{where} 缺少字段 {sorted(missing)}")
        warehouse = _as_str(raw["warehouse"], f"{where}.warehouse")
        location = _as_str(raw["location"], f"{where}.location")
        effective = _as_int(raw["effective_minute"],
                            f"{where}.effective_minute", minimum=0)
        p1 = _as_int(raw["price_tier1"], f"{where}.price_tier1", minimum=0)
        p2 = _as_int(raw["price_tier2"], f"{where}.price_tier2", minimum=0)

        key = (warehouse, location)
        if effective in seen.setdefault(key, set()):
            raise BillingError(
                f"仓 {warehouse!r} 库位 {location!r} 的生效分钟 {effective} 重复")
        seen[key].add(effective)
        grouped.setdefault(key, []).append(
            RateEntry(effective_minute=effective,
                      price_tier1=p1, price_tier2=p2))

    for entries in grouped.values():
        entries.sort(key=lambda e: e.effective_minute)
    return PriceBook(threshold_minutes=threshold, rates=grouped)


def parse_period(value_start, value_end) -> tuple[int, int]:
    """账期 [start, end)：整数分钟，0 <= start < end。"""
    def _one(value, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise BillingError(
                f"账期{name}必须是整数，实际为 {type(value).__name__}: {value!r}")
        if value < 0:
            raise BillingError(f"账期{name}不能为负：{value}")
        return value

    start = _one(value_start, "起点 start")
    end = _one(value_end, "末端 end")
    if not start < end:
        raise BillingError(f"账期必须满足 0 <= start < end，实际为 [{start}, {end})")
    return start, end
