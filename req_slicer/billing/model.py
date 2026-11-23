"""离线仓储计费：数据结构层。

所有时间都是统一基准上的非负整数**分钟**，账期为半开区间 ``[start, end)``：
分钟 ``t`` 指区间 ``[t, t+1)``。正常在库（NORMAL）的原料段每经历一个这样的
分钟累计 1 分钟“正常在库时长”；隔离（ISOLATED）与领料占用（ISSUED）期间
暂停累计，放行/退料回到正常库存后接着累计；移位只换库位，时长不清零。

计费全程只处理**整数区段与区间对象**：一件料的经历表达为若干条
``(库位, 起始分钟, 末端分钟, 区间开始时的累计时长)`` 的在库片段（spell），
绝不按件或按分钟展开。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Optional


class BillingError(Exception):
    """计费失败（流水时间/价目表/账期非法、应收费时间缺价等）。msg 面向使用者。"""


# 两档单价在明细里的固定顺序
TIER1 = "tier1"
TIER2 = "tier2"


@dataclass(frozen=True)
class RateEntry:
    """一条库位费率：``effective_minute`` 起生效（含），同库位内分钟不重复。"""

    effective_minute: int
    price_tier1: int   # 微元/件/分钟，非负整数
    price_tier2: int   # 微元/件/分钟，非负整数


@dataclass(frozen=True)
class PriceBook:
    """价目表：统一门槛 + 按 (仓, 库位) 的生效分钟费率表（分钟升序、不重复）。"""

    threshold_minutes: int
    rates: dict[tuple[str, str], list[RateEntry]]

    def _entries(self, warehouse: str, location: str) -> Optional[list[RateEntry]]:
        return self.rates.get((warehouse, location))

    def rate_entry_index(self, warehouse: str, location: str,
                         minute: int) -> tuple[Optional[RateEntry], Optional[int]]:
        """分钟 ``minute`` 当时最新生效的费率及其在该库位表中的下标。

        该库位没有任何不晚于该分钟的生效记录时返回 ``(None, None)``，由调用
        方按“缺价”处理；下标用于 :meth:`next_change_minute` 找下一次改价分钟。
        """
        entries = self._entries(warehouse, location)
        if not entries:
            return None, None
        keys = [e.effective_minute for e in entries]
        idx = bisect_right(keys, minute) - 1
        if idx < 0:
            return None, None
        return entries[idx], idx

    def next_change_minute(self, warehouse: str, location: str,
                           index: int) -> Optional[int]:
        """下标 ``index`` 的费率之后下一次改价的生效分钟；没有则 None。"""
        entries = self._entries(warehouse, location)
        if entries is None or index + 1 >= len(entries):
            return None
        return entries[index + 1].effective_minute

    def tier_of(self, accrued_minutes: int) -> str:
        """按某分钟**开始时**的累计时长选档；达到门槛即用第二档。"""
        return TIER2 if accrued_minutes >= self.threshold_minutes else TIER1


@dataclass(frozen=True)
class Spell:
    """一段原料在某库位正常在库的连续分钟区间 ``[start_minute, end_minute)``。

    ``base_accrued`` 是该区间第一分钟开始时已经累计的正常在库时长（可能来自
    期前经历）；因此区间内第 ``t`` 分钟开始时累计时长为
    ``base_accrued + (t - start_minute)``。隔离/领料会结束 spell，放行/退料
    另开新 spell 并带上当时的累计值；移位在新库位另开 spell，累计值连续。
    """

    warehouse: str
    material: str
    location: str
    receipt_seq: int
    seg_start: int
    seg_end: int
    start_minute: int
    end_minute: int
    base_accrued: int

    @property
    def qty(self) -> int:
        return self.seg_end - self.seg_start

    @property
    def minutes(self) -> int:
        return self.end_minute - self.start_minute


@dataclass
class GroupCharge:
    """一个 (仓, 物料, 原收料动作序号, 库位) 分组的两档件分钟与费用。"""

    warehouse: str
    material: str
    receipt_seq: int
    location: str
    piece_minutes: dict[str, int] = field(
        default_factory=lambda: {TIER1: 0, TIER2: 0})
    fee: dict[str, int] = field(
        default_factory=lambda: {TIER1: 0, TIER2: 0})

    @property
    def fee_total(self) -> int:
        return self.fee[TIER1] + self.fee[TIER2]

    def as_json(self) -> dict:
        return {
            "warehouse": self.warehouse,
            "material": self.material,
            "receipt_seq": self.receipt_seq,
            "location": self.location,
            "piece_minutes": {
                TIER1: self.piece_minutes[TIER1],
                TIER2: self.piece_minutes[TIER2],
            },
            "fee_micro": {
                TIER1: self.fee[TIER1],
                TIER2: self.fee[TIER2],
            },
            "fee_total_micro": self.fee_total,
        }


@dataclass
class BillingReport:
    """一次账期计费的完整结果。无占用时 details 为空、总额为 0。"""

    period_start: int
    period_end: int
    threshold_minutes: int
    details: list[GroupCharge] = field(default_factory=list)

    @property
    def total_fee_micro(self) -> int:
        return sum(g.fee_total for g in self.details)

    def as_json(self) -> dict:
        return {
            "period": {"start": self.period_start, "end": self.period_end},
            "threshold_minutes": self.threshold_minutes,
            "details": [g.as_json() for g in self.details],
            "total_fee_micro": self.total_fee_micro,
        }
