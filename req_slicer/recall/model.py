"""召回影响分析：数据结构层。

所有影响量都以*区段*（半开整数区间 [start, end)）表达，绝不为区段内的
件数展开对象；同一份覆盖的相交/相邻区段先合并再计量，因此内存与工作量
只随区段个数增长，不随件数增长。

召回影响图是一棵有向森林：
- 外部到货收料 -> 领料实际取到的原料段（交付行）
- 领料 -> 它归属的生产单实际投料区段（生产收料上的坐标）
- 中间品收料 -> 再领用时实际取到的区段
每个节点的冻结区段去重并合并相邻/重叠项，沿一条路径不重复计量。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class RecallError(Exception):
    """召回分析失败（输入/引用/工艺校验不通过）。msg 面向使用者。"""


Range = tuple[int, int]


# ---------------------------------------------------------------------------
# 区段运算：只处理少量 (start, end) 对象，不对件数做任何展开
# ---------------------------------------------------------------------------
def normalize(ranges) -> list[Range]:
    """合并相交或相邻的区段，返回按起点排序、互不重叠的区段列表。"""
    ordered = sorted((int(s), int(e)) for s, e in ranges if int(e) > int(s))
    out: list[Range] = []
    for start, end in ordered:
        if out and start <= out[-1][1]:  # 相交或恰好相邻
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def intersect(a: list[Range], b: list[Range]) -> list[Range]:
    """两个已规范化的区段列表的交集（结果已规范化）。"""
    out: list[Range] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def total(ranges) -> int:
    return sum(end - start for start, end in ranges)


@dataclass(frozen=True)
class Ref:
    """领料/收料的全局定位键：批次显示名、单据显示名、单独序号都不是全局键。

    只有 (仓, 物料, 动作序号) 三元组能唯一定位一笔领料或一笔收料。
    """

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


@dataclass
class IssueNode:
    """一笔领料被召回覆盖的原料段。

    segments 按“上游收料序号”分组：一笔领料可能同时取到多批原料，
    保序加工要按交付行原顺序把这批子片连成坐标轴，所以不能先跨批次
    合并——只有同一批次内、多条来路指向同一段的才合并。
    """

    ref: Ref
    doc_no: str
    segments: dict[int, list[Range]] = field(default_factory=dict)


@dataclass
class ReceiptNode:
    """一笔收料被召回覆盖的区段，以及声明它为产出的生产单。"""

    ref: Ref
    segments: list[Range] = field(default_factory=list)
    work_order: "WorkOrder | None" = None
    external: bool = False
    full_length: int = 0               # 原收料原料段长度 [0, full_length)


@dataclass
class WorkOrder:
    doc_no: str
    process: str                       # "ordered" / "mixed"
    issues: list[Ref] = field(default_factory=list)
    receipts: list[Ref] = field(default_factory=list)
