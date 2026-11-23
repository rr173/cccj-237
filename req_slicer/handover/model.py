"""离线交接对账：数据结构层。

所有数量都以*半开整数区段*表达。发货坐标是一笔领料自其首次取走时开始、
按实际交付次序连续编号的“交付坐标”；收料坐标是收料自身原料段坐标。
对账全程只处理区段对象，**不按件展开**任何流水、凭证或清单。

术语
----
- 调拨清单（transfer）：一票一次发仓 -> 收仓的交接，带唯一调拨号；
  发仓侧是领料引用，收仓侧是指定的收料仓。
- 凭证（voucher）：承运商补交/改签/作废的签收记录，带非负整数发布号。
  同一凭证号在发布号上限内以最高发布号为准；作废不认领任何货。
- 生效凭证（effective voucher）：不超过上限、且是其凭证号在本次上限内
  最高发布号的凭证；作废版本没有内容，因而不进入认领。
"""

from __future__ import annotations

from dataclasses import dataclass, field

Range = tuple[int, int]


class HandoverError(Exception):
    """交接对账失败（输入/引用/凭证矛盾/签收非法）。msg 面向使用者。"""


# ---------------------------------------------------------------------------
# 区段运算：只处理少量 (start, end) 对象
# ---------------------------------------------------------------------------
def normalize(ranges) -> list[Range]:
    """合并相交或相邻的区段，返回按起点排序、互不重叠的区段列表。"""
    ordered = sorted((int(s), int(e)) for s, e in ranges if int(e) > int(s))
    out: list[Range] = []
    for start, end in ordered:
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def subtract(ranges, cut) -> list[Range]:
    """ranges 减去 cut；两个输入先各自取并集，结果按起点排序。"""
    base = normalize(ranges)
    cut = normalize(cut)
    out: list[Range] = []
    j = 0
    for start, end in base:
        cur = start
        while j < len(cut) and cut[j][1] <= cur:
            j += 1
        k = j
        while k < len(cut) and cut[k][0] < end:
            lo, hi = cut[k]
            if lo > cur:
                out.append((cur, min(lo, end)))
            cur = max(cur, hi)
            if cur >= end:
                break
            k += 1
        if cur < end:
            out.append((cur, end))
    return out


def overlap(a: Range, b: Range) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def total(ranges) -> int:
    return sum(e - s for s, e in ranges)


# ---------------------------------------------------------------------------
# 定位键
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Ref:
    """一笔领料/收料的全局定位键：(仓, 物料, 动作序号)。"""

    warehouse: str
    material: str
    action_seq: int

    def as_json(self) -> dict:
        return {"warehouse": self.warehouse, "material": self.material,
                "action_seq": self.action_seq}

    def sort_key(self) -> tuple:
        return (self.warehouse, self.material, self.action_seq)

    def __str__(self) -> str:
        return f"({self.warehouse}, {self.material}, seq={self.action_seq})"


@dataclass(frozen=True)
class Transfer:
    """一票调拨：发仓领料 -> 指定收仓。"""

    doc_no: str
    issue: Ref
    dest_warehouse: str


@dataclass
class ReceiptSection:
    """一条生效签收落在收料上的区段（收料自身原料段坐标）。"""

    receipt: Ref
    start: int
    end: int

    def as_json(self) -> dict:
        return {"receipt": self.receipt.as_json(),
                "start": self.start, "end": self.end}


@dataclass
class VoucherClaim:
    """一条生效签收对一个调拨的认领。

    ship_start/ship_end 是发货坐标（交付坐标）上的半开区段；
    收料侧可以是一条完整签收（receipts 长度 1），也可以是分段签收的
    连续拼接（receipts 按拼接顺序排列，各段首尾相接）。
    unreturned_qty 是这条认领在发货侧仍然有效（未落在退料区）的数量。
    """

    voucher_no: str
    publish_no: int
    doc_no: str
    ship_start: int
    ship_end: int
    receipts: list[ReceiptSection] = field(default_factory=list)
    unreturned_qty: int = 0

    def receipt_json(self) -> list[dict]:
        return [r.as_json() for r in self.receipts]


@dataclass
class Segment:
    """对账清单的一段：发货坐标、收货证据共同决定的最大连续段。"""

    ship_start: int
    ship_end: int
    status: str                    # RETURNED / PENDING / RECEIVED / DISPUTED
    claims: list[VoucherClaim] = field(default_factory=list)
    source_receipt: Ref | None = None       # RECEIVED 且有来源时的收料
    source_start: int | None = None
    source_end: int | None = None
    others: list[str] = field(default_factory=list)  # 收料段上的其他认领凭证号
    conflict: dict | None = None            # DISPUTED 时的争议坐标明细

    @property
    def qty(self) -> int:
        return self.ship_end - self.ship_start

    def as_json(self) -> dict:
        out: dict = {
            "ship": {"start": self.ship_start, "end": self.ship_end,
                     "qty": self.qty},
            "status": self.status,
        }
        if self.claims:
            out["vouchers"] = [
                {"voucher_no": c.voucher_no, "publish_no": c.publish_no,
                 "ship": {"start": c.ship_start, "end": c.ship_end,
                          "unreturned_qty": c.unreturned_qty},
                 "receipt_segments": c.receipt_json()}
                for c in sorted(self.claims,
                                key=lambda c: (c.voucher_no, c.publish_no))]
        if self.status == RECEIVED and self.source_receipt is not None:
            out["source"] = {
                "receipt": self.source_receipt.as_json(),
                "start": self.source_start, "end": self.source_end,
            }
        if self.status == DISPUTED and self.conflict:
            out["conflict"] = self.conflict
        out["other_claim_vouchers"] = sorted(set(self.others))
        return out


@dataclass
class TransferReport:
    doc_no: str
    issue: Ref
    dest_warehouse: str
    ship_qty: int
    segments: list[Segment] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "doc_no": self.doc_no,
            "issue": self.issue.as_json(),
            "dest_warehouse": self.dest_warehouse,
            "ship_qty": self.ship_qty,
            "segments": [s.as_json() for s in self.segments],
        }


@dataclass
class ReconciliationReport:
    publish_limit: int
    transfers: list[TransferReport] = field(default_factory=list)

    def as_json(self) -> dict:
        return {"publish_limit": self.publish_limit,
                "transfers": [t.as_json() for t in self.transfers]}


# ---- 状态（清单输出用英文常量，文档给出中文对照） ----
RETURNED = "RETURNED"      # 已退回：发货坐标落在退料区，不参与销账
PENDING = "PENDING"        # 待签收：无生效认领
RECEIVED = "RECEIVED"      # 已签收：唯一生效凭证，收料位置也无人争用
DISPUTED = "DISPUTED"      # 争议：发货或收料任一边被多个生效凭证认领
