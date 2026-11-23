"""批次切片模型与单物料流水解释引擎。

设计要点
========
1. 一笔收料(receive)产生一个*原料段* [0, qty)，后续所有拆分都以
   (原收料序号, 原料段起点, 原料段末端) 表达，可逐段核验，不复制数量。
2. 切片(SLice)只记录整数数量和整数区段，不按件展开——
   数量数百万的切片始终是一个对象，不会变成一百万行。
3. 同一物料同一仓的动作按 seq 执行；不同物料/仓互不影响。
4. 正常库存的选取键固定为 (货龄起点, 原收料序号, 原料段起点)，
   部分裁取一律从被选中切片的区段起点开始。
5. 可领用/可移动的只有 NORMAL(在库) 切片；隔离切片由 ISOLATED 持有；
   已被领料取走的切片由 ISSUED 持有，只有退料能让它回到在库。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

# ---- 动作类型 ----
RECEIVE = "receive"      # 收料：建立批次身份/货龄起点/原料段
TRANSFER = "transfer"    # 移位：库位 -> 库位，库内正常库存
ISOLATE = "isolate"      # 移入隔离笼
RELEASE = "release"      # 隔离放行：回到指定库位的正常库存
ISSUE = "issue"          # 领料
RETURN = "return"        # 退料（必须引用一笔领料）

# ---- 切片状态 ----
NORMAL = "NORMAL"        # 尚在库（某库位的正常库存）
ISOLATED = "ISOLATED"    # 在隔离笼中，由某次隔离动作持有
ISSUED = "ISSUED"        # 已被领料占用，由某笔领料持有


class FlowError(Exception):
    """流水不合法。msg 为面向使用者的断点说明。"""


@dataclass(frozen=True)
class Slice:
    """不可变批次切片。

    身份三元组 (receipt_seq, seg_start, seg_end) 全局定位原料段位置：
    同一收料被拆出的子片区段互不重叠、并集恰为原段。
    """

    material: str
    warehouse: str
    batch: str
    age_start: str              # 货龄起点（收料日期，ISO 字符串，可直接按字典序比较）
    receipt_seq: int            # 原收料序号（动作序号）
    seg_start: int              # 原料段起点（含）
    seg_end: int                # 原料段末端（不含）
    location: Optional[str]     # NORMAL 时的库位；隔离/已领状态为 None
    state: str                  # NORMAL / ISOLATED / ISSUED
    holder: object = None       # ISOLATED -> 隔离动作 id；ISSUED -> 领料单号

    @property
    def qty(self) -> int:
        return self.seg_end - self.seg_start

    @property
    def age_key(self):
        return (self.age_start, self.receipt_seq, self.seg_start)


@dataclass(frozen=True)
class Action:
    """一条归一化后的流水动作。"""

    seq: int
    action_id: str
    op: str
    material: str
    warehouse: str
    batch: Optional[str] = None
    age_start: Optional[str] = None
    qty: Optional[int] = None
    location: Optional[str] = None       # 目标/指定库位（receive/transfer.to/release/return）
    from_location: Optional[str] = None  # transfer/isolate 的来源库位
    isolate_id: Optional[str] = None     # release 引用的隔离动作 id
    ref_doc: Optional[str] = None        # return 引用的领料单号
    minute: Optional[int] = None         # 统一时间基准（非负整数分钟）；离线计费需要
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryLine:
    """领料交付表的一行：一笔领料实际取走的一个批次切片。"""

    line_id: int               # 台账内唯一行号（退料按它定位，避免坐标重复时错位）
    doc_no: str
    issue_seq: int
    material: str
    warehouse: str
    batch: str
    age_start: str
    receipt_seq: int
    seg_start: int
    seg_end: int
    qty: int
    returned_qty: int          # 截至期末该交付行已被退回的数量
    outstanding_qty: int       # 仍由该领料占用的数量


@dataclass
class _Outstanding:
    """一笔领料交付行的未退回区间（在切片区段坐标系内）。"""

    line: DeliveryLine
    start: int
    end: int

    @property
    def qty(self) -> int:
        return self.end - self.start


def _insert_normal(store: list, sl: Slice) -> None:
    """按 (货龄起点, 原收料序号, 原料段起点) 有序插入正常库存。"""
    key = sl.age_key
    lo, hi = 0, len(store)
    while lo < hi:
        mid = (lo + hi) // 2
        if store[mid].age_key < key:
            lo = mid + 1
        else:
            hi = mid
    store.insert(lo, sl)


class MaterialLedger:
    """解释 (仓, 物料) 维度的一串动作，产出交付明细与期末切片。"""

    def __init__(self, material: str, warehouse: str):
        self.material = material
        self.warehouse = warehouse

        self.normal: list[Slice] = []                 # 有序正常库存
        self.isolated: dict[str, list[Slice]] = {}    # 隔离动作 id -> 其仍持有的切片（按原顺序）
        self.issued: dict[str, list[Slice]] = {}      # 领料单号 -> 仍被其占用的切片
        self.lines: list[DeliveryLine] = []           # 全部交付行（含已退部分）
        self.outstanding: dict[str, list[_Outstanding]] = {}  # 领料单 -> 未退回区间
        self.issue_docs: dict[int, str] = {}          # 领料动作序号 -> 单据号（召回定位用）
        self.receipts: dict[int, Slice] = {}          # 收料序号 -> 原始整段（对账用）
        self.applied_ids: set[str] = set()            # 已执行动作 id（幂等）

    # ------------------------------------------------------------------
    # 两阶段裁取：先做只读计划，数量不足直接抛错且不留副作用
    # ------------------------------------------------------------------
    def _plan_cut(self, pool: list[Slice], qty: int, *,
                  location: Optional[str] = None,
                  holder_ok: bool = False) -> list[tuple[Slice, int, int]]:
        """从有序池中按固定顺序规划裁取，返回 (切片, 段起点, 段末端) 计划。"""
        if qty <= 0:
            raise FlowError("动作数量必须为正整数")
        plan: list[tuple[Slice, int, int]] = []
        remain = qty
        for sl in pool:
            if remain == 0:
                break
            if sl.state != NORMAL and not holder_ok:
                continue
            if location is not None and sl.location != location:
                continue
            take = min(remain, sl.qty)
            # 部分裁取从区段起点开始
            plan.append((sl, sl.seg_start, sl.seg_start + take))
            remain -= take
        if remain:
            where = f"库位 {location}" if location is not None else "全部在库"
            raise FlowError(f"数量不足：需要 {qty}，{where} 可用 {qty - remain}，缺 {remain}")
        return plan

    @staticmethod
    def _apply_cut(pool: list[Slice], plan: list[tuple[Slice, int, int]]) -> list[Slice]:
        """按计划从池中移除/收缩原切片，返回裁下的同身份子片（保留批次与货龄）。"""
        cut: list[Slice] = []
        for sl, lo, hi in plan:
            idx = pool.index(sl)
            pool.pop(idx)
            if lo > sl.seg_start:
                pool.insert(idx, replace(sl, seg_end=lo))
            if hi < sl.seg_end:
                pool.insert(idx + (1 if lo > sl.seg_start else 0),
                            replace(sl, seg_start=hi))
            cut.append(replace(sl, seg_start=lo, seg_end=hi,
                               location=None, state=ISSUED, holder=None))
        return cut

    # ------------------------------------------------------------------
    def apply(self, act: Action) -> None:
        if act.material != self.material or act.warehouse != self.warehouse:
            raise FlowError("内部错误：动作与台账物料/仓不匹配")
        if act.action_id in self.applied_ids:
            raise FlowError("内部错误：重复动作应在上游过滤")
        handler = {
            RECEIVE: self._receive,
            TRANSFER: self._transfer,
            ISOLATE: self._isolate,
            RELEASE: self._release,
            ISSUE: self._issue,
            RETURN: self._return,
        }.get(act.op)
        if handler is None:
            raise FlowError(f"未知动作类型 {act.op!r}")
        handler(act)
        self.applied_ids.add(act.action_id)

    def _receive(self, act: Action) -> None:
        if not act.batch or not act.age_start or not act.qty or not act.location:
            raise FlowError("收料动作缺少批次、货龄起点、数量或库位")
        if act.seq in self.receipts:  # 防御：正常情况下 seq 唯一性已由上游保证
            raise FlowError(f"收料序号 {act.seq} 冲突")
        sl = Slice(material=act.material, warehouse=act.warehouse, batch=act.batch,
                   age_start=act.age_start, receipt_seq=act.seq,
                   seg_start=0, seg_end=act.qty, location=act.location, state=NORMAL)
        _insert_normal(self.normal, sl)
        self.receipts[act.seq] = sl

    def _transfer(self, act: Action) -> None:
        if not act.from_location or not act.location:
            raise FlowError("移位动作需要来源库位与目标库位")
        plan = self._plan_cut(self.normal, act.qty, location=act.from_location)
        cut = self._apply_cut(self.normal, plan)
        for sl in cut:  # 库位变更不改变批次身份、货龄与原料段位置
            _insert_normal(self.normal, replace(sl, location=act.location, state=NORMAL))

    def _isolate(self, act: Action) -> None:
        if not act.from_location:
            raise FlowError("隔离动作需要来源库位")
        if act.action_id in self.isolated:
            raise FlowError(f"隔离动作身份 {act.action_id} 重复")
        plan = self._plan_cut(self.normal, act.qty, location=act.from_location)
        cut = self._apply_cut(self.normal, plan)
        held = [replace(sl, state=ISOLATED, holder=act.action_id) for sl in cut]
        self.isolated[act.action_id] = held  # 池顺序即原顺序，天然有序

    def _release(self, act: Action) -> None:
        if not act.isolate_id or not act.location:
            raise FlowError("放行动作需要隔离动作身份与回到的库位")
        held = self.isolated.get(act.isolate_id)
        if held is None:
            raise FlowError(f"放行引用的隔离动作 {act.isolate_id} 不存在")
        if not held:
            raise FlowError(f"隔离动作 {act.isolate_id} 已无持有的切片可放行")
        # 只允许该隔离动作尚持有的切片，在其持有库存中按原顺序取
        plan = self._plan_cut(held, act.qty, holder_ok=True)
        cut = self._apply_cut(held, plan)
        for sl in cut:
            _insert_normal(self.normal,
                           replace(sl, location=act.location, state=NORMAL, holder=None))

    def _issue(self, act: Action) -> None:
        if not act.ref_doc and not act.action_id:
            raise FlowError("领料动作缺少单据号")
        doc = act.ref_doc or act.action_id
        if act.seq in self.issue_docs:  # 防御：序号唯一性由上游保证
            raise FlowError(f"领料序号 {act.seq} 冲突")
        plan = self._plan_cut(self.normal, act.qty)  # 全仓正常库存，按货龄顺序
        cut = self._apply_cut(self.normal, plan)
        held: list[Slice] = []
        outs: list[_Outstanding] = []
        for sl in cut:
            moved = replace(sl, state=ISSUED, holder=doc)
            held.append(moved)
            line = DeliveryLine(
                line_id=len(self.lines),
                doc_no=doc, issue_seq=act.seq, material=act.material,
                warehouse=act.warehouse, batch=sl.batch, age_start=sl.age_start,
                receipt_seq=sl.receipt_seq, seg_start=sl.seg_start, seg_end=sl.seg_end,
                qty=sl.qty, returned_qty=0, outstanding_qty=sl.qty)
            self.lines.append(line)
            outs.append(_Outstanding(line, sl.seg_start, sl.seg_end))
        self.issued[doc] = self.issued.get(doc, []) + held
        self.outstanding[doc] = self.outstanding.get(doc, []) + outs
        self.issue_docs[act.seq] = doc

    def _return(self, act: Action) -> None:
        if not act.ref_doc or not act.location:
            raise FlowError("退料动作需要引用的领料单号与退回库位")
        outs = self.outstanding.get(act.ref_doc)
        if not outs:
            raise FlowError(f"退料引用的领料单 {act.ref_doc} 不存在或已全部退回")
        total = sum(o.qty for o in outs)
        if act.qty > total:
            raise FlowError(
                f"超量退料：领料单 {act.ref_doc} 未退回 {total}，本次退 {act.qty}")

        remain = act.qty
        # 从最后实际取走的切片向前退；切片内由区段末端向前退
        for o in reversed(outs):
            if remain == 0:
                break
            take = min(remain, o.qty)
            hi = o.end
            lo = hi - take
            # 从 ISSUED 持有列表中剥出 [lo, hi)，余下的仍是该领料占用
            peeled = self._peel_issued(self.issued[act.ref_doc], o.line, lo, hi)
            for sl in peeled:
                _insert_normal(self.normal,
                               replace(sl, location=act.location, state=NORMAL, holder=None))
            # 维护未退回区间与交付行累计数
            idx = outs.index(o)
            if lo == o.start and hi == o.end:
                outs.pop(idx)
            else:
                outs[idx] = _Outstanding(o.line, o.start, lo)
            line = o.line
            self._bump_returned(line, take)
            remain -= take
        if remain:  # 理论上不可达（前面已校验），防御性保留
            raise FlowError("退料处理异常：数量未退完")
        if not outs:
            self.outstanding.pop(act.ref_doc, None)

    @staticmethod
    def _peel_issued(pool: list[Slice], line: DeliveryLine,
                     lo: int, hi: int) -> list[Slice]:
        """从领料占用池中剥出交付行原料段内的 [lo, hi) 区段。

        交付行坐标是该领料*首次取走时*的整段坐标；经过部分退料后，池中
        对应的可能已是若干子片，因此这里按“属于本交付行原料段且与目标
        区间相交”的池切片逐个处理，而不是要求整段仍在池中。
        剩余子片继续留在池中，仍由该领料占用。
        """
        out: list[Slice] = []
        i = 0
        while i < len(pool):
            sl = pool[i]
            same_line = (sl.receipt_seq == line.receipt_seq
                         and sl.seg_start >= line.seg_start
                         and sl.seg_end <= line.seg_end)
            if not same_line or sl.seg_end <= lo or sl.seg_start >= hi:
                i += 1
                continue
            pool.pop(i)
            leftovers: list[Slice] = []
            cut_lo, cut_hi = max(lo, sl.seg_start), min(hi, sl.seg_end)
            if cut_lo > sl.seg_start:
                leftovers.append(replace(sl, seg_end=cut_lo))
            if cut_hi < sl.seg_end:
                leftovers.append(replace(sl, seg_start=cut_hi))
            pool[i:i] = leftovers
            out.append(replace(sl, seg_start=cut_lo, seg_end=cut_hi))
            i += len(leftovers) + 1
        if not out:
            raise FlowError("退料处理异常：找不到领料占用切片")
        return out

    def _bump_returned(self, line: DeliveryLine, take: int) -> None:
        # 交付行在累计退回时会被整体替换，按唯一行号定位当前那一行
        idx = next(i for i, cur in enumerate(self.lines)
                   if cur.line_id == line.line_id)
        cur = self.lines[idx]
        self.lines[idx] = replace(cur,
                                  returned_qty=cur.returned_qty + take,
                                  outstanding_qty=cur.outstanding_qty - take)

    # ------------------------------------------------------------------
    # 期末视图与对账
    # ------------------------------------------------------------------
    def live_slices(self) -> list[Slice]:
        """期末全部未消费切片：在库 + 隔离 + 仍被领料占用。"""
        out = list(self.normal)
        for held in self.isolated.values():
            out.extend(held)
        for held in self.issued.values():
            out.extend(held)
        out.sort(key=lambda s: (s.state, s.receipt_seq, s.seg_start))
        return out

    def reconciliation(self) -> dict:
        """数量守恒与原料段覆盖核对。"""
        live = self.live_slices()
        by_receipt: dict[int, list[Slice]] = {}
        for sl in live:
            by_receipt.setdefault(sl.receipt_seq, []).append(sl)

        coverage_errors: list[str] = []
        for seq, original in self.receipts.items():
            parts = sorted(by_receipt.get(seq, []), key=lambda s: s.seg_start)
            cursor = 0
            for p in parts:
                if p.batch != original.batch or p.age_start != original.age_start:
                    coverage_errors.append(
                        f"收料 {seq} 的子片批次/货龄与原收料不一致")
                if p.seg_start != cursor:
                    coverage_errors.append(
                        f"收料 {seq} 原料段出现缺口或重叠：期望起点 {cursor}，实际 {p.seg_start}")
                cursor = p.seg_end
            if cursor != original.seg_end:
                coverage_errors.append(
                    f"收料 {seq} 原料段覆盖不完整：末端 {cursor} != {original.seg_end}")

        issued_total = sum(o.qty for os_ in self.outstanding.values() for o in os_)
        state_totals = {
            NORMAL: sum(s.qty for s in self.normal),
            ISOLATED: sum(s.qty for h in self.isolated.values() for s in h),
            ISSUED: sum(s.qty for h in self.issued.values() for s in h),
        }
        return {
            "material": self.material,
            "warehouse": self.warehouse,
            "received_total": sum(s.qty for s in self.receipts.values()),
            "state_totals": state_totals,
            "issued_outstanding_total": issued_total,
            "delivery_total": sum(l.qty for l in self.lines),
            "returned_total": sum(l.returned_qty for l in self.lines),
            "live_total": sum(s.qty for s in live),
            "conserved": (state_totals[NORMAL] + state_totals[ISOLATED]
                          + state_totals[ISSUED] == sum(
                              s.qty for s in self.receipts.values())),
            "coverage_ok": not coverage_errors,
            "coverage_errors": coverage_errors,
        }
