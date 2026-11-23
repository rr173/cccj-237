"""离线仓储计费引擎（离线、纯标准库）。

只读重放：仓内流水先照常由现有引擎解释（断点/幂等/序号规则完全复用），
本模块再按同一套去重排序结果带时间重放，**不改变任何库存**，也不做跨仓
归集或发票。

时间与在库时长语义
==================
1. 每条动作带统一时间基准上的非负整数 ``minute``；同 (仓, 物料) 内按原动作
   序号执行，时间不得倒退（``minute`` 非递减）；重送动作的 ``minute`` 也须
   相同（计入内容签名）。同一分钟内的动作按序生效，动作之间不计时间。
2. 每个存活原料段只累计**正常在库**时间：隔离与领料期间暂停；放行与退料
   回到正常库存后接着累计；移位后费用归新库位但累计时长不清零。
3. 分钟 ``t`` 指半开区间 ``[t, t+1)``，该分钟的状态是分钟 ``t`` 起点动作
   全部生效后的状态。账期 ``[start, end)`` 只对 ``start <= t < end`` 收费；
   恰在 ``end`` 的动作可验证、不收费。期前流水正常重放，用来算期初状态与
   累计时长，本身不收费。
4. 每分钟用当时最新生效的费率，按该分钟**开始时**的累计时长选档，达到
   ``threshold_minutes`` 即用第二档。无仓库动作也可能跨档或改价——这两类
   边界在计费时按区间处理，不依赖动作分钟。

实现
====
库存裁断全部委托规范的 ``MaterialLedger``（FIFO/区段语义与 ``run`` 完全
一致）；本模块只为每个存活切片维护一条开放的“正常在库 spell”侧记录。
切片被拆分时，spell 按拆分边界同步切成残片 spell（保留原起点与累计值）与
子片 spell，因此一次动作只动它实际触及的切片，不重扫无关库存，也不按件、
按分钟展开。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.inputs import parse_file, InputError
from ..engine.model import (Action, MaterialLedger, FlowError,
                            RECEIVE, TRANSFER, ISOLATE, RELEASE, ISSUE,
                            RETURN, NORMAL, ISOLATED, ISSUED)
from ..engine.runner import run, plan_groups
from .inputs import parse_price_book, parse_period
from .model import (BillingError, PriceBook, Spell, GroupCharge,
                    BillingReport, TIER1)


def _key(sl) -> tuple[int, int, int]:
    """存活切片的身份键：(原收料序号, 段起点, 段末端)。"""
    return (sl.receipt_seq, sl.seg_start, sl.seg_end)


def _snapshot(ledger: MaterialLedger) -> object:
    """期末切片的状态指纹，用于验证计费重放与规范引擎解释一致。"""
    rows = []
    for sl in ledger.normal:
        rows.append((NORMAL, sl.receipt_seq, sl.seg_start, sl.seg_end,
                     sl.location, sl.holder))
    for holder, held in ledger.isolated.items():
        for sl in held:
            rows.append((ISOLATED, sl.receipt_seq, sl.seg_start, sl.seg_end,
                         sl.location, holder))
    for holder, held in ledger.issued.items():
        for sl in held:
            rows.append((ISSUED, sl.receipt_seq, sl.seg_start, sl.seg_end,
                         sl.location, holder))
    return sorted(rows)


@dataclass
class _OpenSpell:
    """一条仍在持续的正常在库 spell（侧表，不修改切片本身）。

    ``start``/``base`` 是这段正常在库经历的起点分钟与起点累计时长；切片在
    暂停前可能已经被多次拆分，残片共享同一条经历，拆分时各自复制本对象。
    """

    location: str
    start: int
    base: int


class BillingLedger:
    """带时间重放一条 (仓, 物料) 流水，产出正常在库 :class:`Spell` 列表。

    侧表（键为切片身份键）：

    - ``open_spell``：当前 NORMAL 切片的开放 spell；
    - ``accrued``：当前暂停（ISOLATED/ISSUED）切片的累计正常分钟数；
    - ``closed``：已经结束的 spell（期末再统一关闭仍开放的）。
    """

    def __init__(self, ledger: MaterialLedger):
        self.ledger = ledger
        self.material = ledger.material
        self.warehouse = ledger.warehouse
        self.open_spell: dict[tuple[int, int, int], _OpenSpell] = {}
        self.accrued: dict[tuple[int, int, int], int] = {}
        self.closed: list[Spell] = []

    # ------------------------------------------------------------------
    def replay(self, actions: list[Action], horizon: int) -> list[Spell]:
        for act in actions:
            self._step(act)
        for sl in list(self.ledger.normal):
            opn = self.open_spell[_key(sl)]
            if horizon > opn.start:
                self.closed.append(self._spell(sl, opn, horizon))
        return self.closed

    def _spell(self, sl, opn: _OpenSpell, end_minute: int) -> Spell:
        return Spell(
            warehouse=self.warehouse, material=self.material,
            location=opn.location, receipt_seq=sl.receipt_seq,
            seg_start=sl.seg_start, seg_end=sl.seg_end,
            start_minute=opn.start, end_minute=end_minute,
            base_accrued=opn.base)

    def _accrued_at(self, sl, minute: int) -> tuple[int, _OpenSpell | None]:
        """切片在 minute 被触及前的累计时长；NORMAL 同时取出并移除开放 spell。"""
        k = _key(sl)
        opn = self.open_spell.pop(k, None)
        if opn is not None:
            if minute > opn.start:
                self.closed.append(self._spell(sl, opn, minute))
            return opn.base + (minute - opn.start), opn
        return self.accrued.pop(k), None

    def _step(self, act: Action) -> None:
        minute = act.minute
        if act.op == RECEIVE:
            self.ledger.apply(act)
            self.open_spell[_key(self.ledger.receipts[act.seq])] = \
                _OpenSpell(act.location, minute, 0)
            return
        if act.op == TRANSFER:
            self._cut(act, minute,
                      self.ledger._plan_cut(self.ledger.normal, act.qty,
                                            location=act.from_location),
                      mode="transfer", new_location=act.location)
            return
        if act.op == ISOLATE:
            self._cut(act, minute,
                      self.ledger._plan_cut(self.ledger.normal, act.qty,
                                            location=act.from_location),
                      mode="pause", holder=act.action_id)
            return
        if act.op == RELEASE:
            held = self.ledger.isolated[act.isolate_id]
            self._cut(act, minute,
                      self.ledger._plan_cut(held, act.qty, holder_ok=True),
                      mode="resume", new_location=act.location)
            return
        if act.op == ISSUE:
            doc = act.ref_doc or act.action_id
            self._cut(act, minute,
                      self.ledger._plan_cut(self.ledger.normal, act.qty),
                      mode="pause", holder=doc)
            return
        if act.op == RETURN:
            self._return(act, minute)
            return
        raise BillingError(f"未知动作类型 {act.op!r}")

    # ------------------------------------------------------------------
    def _cut(self, act: Action, minute: int, plan, *, mode: str,
             new_location: str | None = None, holder: str | None = None) -> None:
        """移位/隔离/放行/领料共用：先结算被裁切片，apply 后按新身份交接。

        关键不变量：NORMAL 来源切片一旦在 minute 被裁，它的旧 spell 就在
        minute 关闭；拆分后所有仍处于 NORMAL 的件（原库位残片、移位/放行子片）
        都从 minute 起另开新 spell，起点累计值取该切片 minute 开始前的累计值，
        因此时长连续但不会与旧 spell 重复计量。暂停来源（放行）的残片继续
        暂停，只带累计值。
        """
        # 1) 结算每个被裁来源切片，登记拆分后的身份与累计值
        pending: list[dict] = []
        for sl, lo, hi in plan:
            base, opn = self._accrued_at(sl, minute)
            pending.append({
                "receipt": sl.receipt_seq, "lo": lo, "hi": hi, "base": base,
                "source_normal": opn is not None,
                "source_location": sl.location,
                "left_key": (sl.receipt_seq, sl.seg_start, lo)
                            if lo > sl.seg_start else None,
                "right_key": (sl.receipt_seq, hi, sl.seg_end)
                             if hi < sl.seg_end else None,
                "child_key": (sl.receipt_seq, lo, hi),
            })
        self.ledger.apply(act)

        holder_pool = None
        if mode == "pause":
            holder_pool = (self.ledger.isolated[holder]
                           if act.op == ISOLATE else self.ledger.issued[holder])

        for item in pending:
            base = item["base"]
            # 残片
            for rk in (item["left_key"], item["right_key"]):
                if rk is None:
                    continue
                if item["source_normal"]:
                    # NORMAL 来源的残片留在原库位：从 minute 起接续累计
                    self.open_spell[rk] = _OpenSpell(
                        item["source_location"], minute, base)
                else:
                    # 暂停来源（放行）的残片继续暂停
                    self.accrued[rk] = base
            ck = item["child_key"]
            if mode in ("transfer", "resume"):
                # 移位/放行子片回正常库存：从 minute 起算，累计时长不清零
                self.open_spell[ck] = _OpenSpell(new_location, minute, base)
            else:  # pause：隔离/领料
                assert _key_in(holder_pool, ck)
                self.accrued[ck] = base

    def _return(self, act: Action, minute: int) -> None:
        # 只读复刻规范引擎的退料剥取（outs 与 issued 池此刻尚未变更），
        # 记录每个被剥子片与残片的身份及暂停前累计值，apply 后再交接。
        outs = self.ledger.outstanding[act.ref_doc]
        pool = self.ledger.issued[act.ref_doc]
        children: list[tuple[tuple, int]] = []
        leftovers: list[tuple[tuple, int]] = []
        remain = act.qty
        for o in reversed(outs):
            if remain == 0:
                break
            take = min(remain, o.qty)
            hi, lo = o.end, o.end - take
            for sl in pool:
                same_line = (sl.receipt_seq == o.line.receipt_seq
                             and sl.seg_start >= o.line.seg_start
                             and sl.seg_end <= o.line.seg_end)
                if not same_line or sl.seg_end <= lo or sl.seg_start >= hi:
                    continue
                cut_lo, cut_hi = max(lo, sl.seg_start), min(hi, sl.seg_end)
                base = self.accrued[_key(sl)]
                children.append(((sl.receipt_seq, cut_lo, cut_hi), base))
                if cut_lo > sl.seg_start:
                    leftovers.append(
                        ((sl.receipt_seq, sl.seg_start, cut_lo), base))
                if cut_hi < sl.seg_end:
                    leftovers.append(
                        ((sl.receipt_seq, cut_hi, sl.seg_end), base))
                self.accrued.pop(_key(sl))
            remain -= take
        self.ledger.apply(act)
        for key, base in leftovers:       # 仍被领料占用：保持暂停
            self.accrued[key] = base
        for key, base in children:        # 退回正常库存：minute 起接着累计
            self.open_spell[key] = _OpenSpell(act.location, minute, base)


def _key_in(pool, key) -> bool:
    return any(_key(sl) == key for sl in pool)


# ---------------------------------------------------------------------------
# 时间校验
# ---------------------------------------------------------------------------
def validate_minutes(ordered: list[Action], horizon: int) -> None:
    """同 (仓, 物料) 内：minute 必填、非负整数、不回退、不晚于账期末端。

    ``horizon`` 即账期末端 end：流水“含截至 end 的动作”，恰在 end 的动作
    仍参与流水校验（可验证、不收费）；minute > end 属于非法时间。
    """
    wh, mat = ordered[0].warehouse, ordered[0].material
    prev = None
    for act in ordered:
        if act.minute is None:
            raise BillingError(
                f"仓 {wh!r} 物料 {mat!r} 动作 seq={act.seq} "
                f"({act.action_id!r}) 缺少 minute（离线计费要求每条动作带整数分钟）")
        if act.minute > horizon:
            raise BillingError(
                f"仓 {wh!r} 物料 {mat!r} 动作 seq={act.seq} "
                f"({act.action_id!r}) 的 minute={act.minute} 晚于账期末端 {horizon}")
        if prev is not None and act.minute < prev:
            raise BillingError(
                f"仓 {wh!r} 物料 {mat!r} 动作 seq={act.seq} "
                f"({act.action_id!r}) 的 minute={act.minute} 早于上一动作的 "
                f"minute={prev}：时间不得倒退")
        prev = act.minute


# ---------------------------------------------------------------------------
# 计费：spell × 账期 × 价目表
# ---------------------------------------------------------------------------
def _charge_spells(spells: list[Spell], book: PriceBook,
                   start: int, end: int) -> list[GroupCharge]:
    groups: dict[tuple, GroupCharge] = {}

    def group_of(sp: Spell) -> GroupCharge:
        key = (sp.warehouse, sp.material, sp.receipt_seq, sp.location)
        gr = groups.get(key)
        if gr is None:
            gr = GroupCharge(warehouse=sp.warehouse, material=sp.material,
                             receipt_seq=sp.receipt_seq, location=sp.location)
            groups[key] = gr
        return gr

    # 按起始分钟、库位、收料、区段排序后计费：缺价时报的一定是最早出现的
    # 应收费时段，定位稳定且便于核对（计费结果本身与遍历顺序无关）。
    for sp in sorted(spells, key=lambda s: (s.start_minute, s.location,
                                            s.receipt_seq, s.seg_start)):
        lo = max(sp.start_minute, start)
        hi = min(sp.end_minute, end)
        if lo >= hi:
            continue  # 与账期不相交（期前片段或期末之后）
        gr = group_of(sp)
        t = lo
        while t < hi:
            accrued_here = sp.base_accrued + (t - sp.start_minute)
            entry, idx = book.rate_entry_index(sp.warehouse, sp.location, t)
            if entry is None:
                raise BillingError(
                    f"仓 {sp.warehouse!r} 库位 {sp.location!r} 在分钟 {t} "
                    f"（时段 [{t}, {t + 1})）缺少有效费率：应收费的正常在库时间"
                    f"没有可用价目")
            # 该费率与该档同时保持不变的最远分钟：下一改价点 / 跨档点 / 区间末端
            seg_hi = hi
            change = book.next_change_minute(sp.warehouse, sp.location, idx)
            if change is not None:
                seg_hi = min(seg_hi, change)
            if accrued_here < book.threshold_minutes:
                cross = (sp.start_minute
                         + (book.threshold_minutes - sp.base_accrued))
                if lo < cross <= hi:
                    seg_hi = min(seg_hi, cross)
            tier = book.tier_of(accrued_here)
            price = entry.price_tier1 if tier == TIER1 else entry.price_tier2
            minutes = seg_hi - t
            gr.piece_minutes[tier] += sp.qty * minutes
            gr.fee[tier] += sp.qty * minutes * price
            t = seg_hi

    return [groups[k] for k in sorted(groups)]


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------
def run_billing(flow_path: str, price_path: str,
                period_start: int, period_end: int) -> BillingReport:
    start, end = parse_period(period_start, period_end)

    try:
        actions = parse_file(flow_path)
    except InputError as exc:
        raise BillingError(f"仓内流水无法解析：{exc}")

    # 1) 规范引擎先跑一遍：任何断点都直接失败，不交部分账单
    result = run(actions)
    broken = [g for g in result.groups if g.error]
    if broken:
        details = "; ".join(
            f"{g.warehouse}/{g.material} seq={g.error_seq}"
            f"({g.error_action_id}): {g.error}" for g in broken)
        raise BillingError(f"仓内流水存在断点：{details}")

    # 2) 价目表（门槛/生效分钟/单价/重复性全部校验）
    book = parse_price_book(price_path)

    # 3) 与 run() 同一套去重排序结果，带时间重放
    try:
        planned = plan_groups(actions)
    except FlowError as exc:  # 理论上 run() 已拦住，防御性保留
        raise BillingError(f"仓内流水非法：{exc}")

    all_spells: list[Spell] = []
    canonical = {(g.warehouse, g.material): g.ledger for g in result.groups}
    for (warehouse, material), ordered in sorted(planned.items()):
        validate_minutes(ordered, end)
        # 用全新台账重放：result 里的台账已被 run() 应用过，只用于断点校验。
        # 期末仍开放的 spell 在账期末端 end 处关闭——收费区间只取 [start,end)，
        # end 及以后的片段会在计费时被裁掉，因此用 end 作关闭视野即可，不依赖
        # 最后一个动作分钟（最后动作可能远早于 end，件在那之后仍持续在库）。
        fresh = MaterialLedger(material, warehouse)
        tracker = BillingLedger(fresh)
        all_spells.extend(tracker.replay(ordered, horizon=end))
        # 计费重放必须与规范引擎得到完全相同的期末切片（同一流水、同一解释）
        if _snapshot(fresh) != _snapshot(canonical[(warehouse, material)]):
            raise BillingError(
                f"内部错误：仓 {warehouse!r} 物料 {material!r} 的计费重放结果与"
                f"规范流水解释不一致")

    # 4) 按账期与价目表计费（缺价在此定位到库位与分钟时段）
    details = _charge_spells(all_spells, book, start, end)
    return BillingReport(period_start=start, period_end=end,
                         threshold_minutes=book.threshold_minutes,
                         details=details)
