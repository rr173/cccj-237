"""交接对账引擎（离线、纯标准库）。

只读：仓内流水照常由现有引擎解释，本模块只读取交付行与收料原始段，
**不改变任何库存**，也不写回生产系统。

语义要点
========
1. **发货坐标**：一笔领料自首次取走起，按实际交付次序连续编号；
   换到下一批料（下一条交付行）编号不断开。退料后编号不变：
   已退回位置仍占坐标、仍出现在清单上，但不参与销账，也不占用收仓签收位置。
2. **签收映射**：一份完整签收把发货区段 [s, e) 等长、顺序不变地映射到
   目标收料从 rec_start 开始的区段；发货位置 p 对应收料位置
   rec_start + (p - s)。落在已退料位置的凭证只在这些位置失效，其余位置
   继续按上述映射对账（不压缩、不重排）。
3. **版本裁定**：`publish_no <= limit` 内，同一凭证号取最高发布号；
   作废版本不认领任何货；同号同发布号内容矛盾直接失败；文件行序无影响。
4. **争议按位置认定**：每个未退回发货位置，恰有一份生效凭证、且映射到的
   收料位置也只有该凭证认领，才算已签收；任一边多凭证认领即争议。
   一笔收料可以被多票合法分段核销；不整单否决、不在冲突中挑赢家。
   收料侧统计所有凭证的未退料部分，包括在发货侧已有争议的凭证。
5. **清单分段**：每票完整覆盖 [0, ship_qty)，在交付行边界、退料边界、
   认领边界与收料侧他人认领边界处切开，输出最大连续段。
6. 全程只处理区段对象，不按件展开流水、凭证或清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..engine.inputs import parse_file, InputError
from ..engine.runner import run
from ..engine.model import MaterialLedger
from .inputs import load_handover, resolve_versions
from .model import (DISPUTED, PENDING, RECEIVED, RETURNED, HandoverError,
                    Range, ReceiptSection, ReconciliationReport, Ref, Segment,
                    Transfer, TransferReport, VoucherClaim, normalize,
                    overlap, subtract, total)


# ---------------------------------------------------------------------------
# 台账视图
# ---------------------------------------------------------------------------
class _LedgerView:
    def __init__(self, run_result):
        self.ledgers: dict[tuple[str, str], MaterialLedger] = {
            (g.warehouse, g.material): g.ledger for g in run_result.groups}

    def ledger(self, warehouse: str, material: str) -> MaterialLedger:
        lg = self.ledgers.get((warehouse, material))
        if lg is None:
            raise HandoverError(
                f"流水里没有 ({warehouse}, {material}) 这个仓/物料的动作")
        return lg

    def issue_lines(self, transfer: Transfer):
        ref = transfer.issue
        lg = self.ledger(ref.warehouse, ref.material)
        doc = lg.issue_docs.get(ref.action_seq)
        if doc is None:
            raise HandoverError(
                f"调拨 {transfer.doc_no!r} 的领料引用 {ref} 在流水中不存在"
                f"（该序号不是领料动作）")
        lines = [l for l in lg.lines
                 if l.issue_seq == ref.action_seq and l.doc_no == doc]
        if not lines:
            raise HandoverError(
                f"调拨 {transfer.doc_no!r} 的领料引用 {ref} 没有交付行")
        return lines

    def receipt_qty(self, ref: Ref, where: str) -> int:
        lg = self.ledger(ref.warehouse, ref.material)
        sl = lg.receipts.get(ref.action_seq)
        if sl is None:
            raise HandoverError(
                f"{where} 的收料引用 {ref} 在流水中不存在（该序号不是收料动作）")
        return sl.seg_end


# ---------------------------------------------------------------------------
# 一票的坐标视图
# ---------------------------------------------------------------------------
@dataclass
class _TransferCtx:
    transfer: Transfer
    lines: list
    ship_qty: int = 0
    line_bounds: list[int] = field(default_factory=list)   # 交付行边界
    returned: list[Range] = field(default_factory=list)    # 发货坐标上的退料区
    signable: list[Range] = field(default_factory=list)    # [0, ship_qty) 未退部分
    claims: list["_Claim"] = field(default_factory=list)


@dataclass
class _Claim:
    voucher_no: str
    publish_no: int
    doc_no: str
    ship_start: int
    ship_end: int
    receipt: Ref
    receipt_start: int
    intervals: list[Range] = field(default_factory=list)   # 未退料的有效发货子区段

    def receipt_range(self, lo: int, hi: int) -> Range:
        base = self.receipt_start - self.ship_start
        return (lo + base, hi + base)


def _build_contexts(view: _LedgerView,
                    transfers: list[Transfer]) -> dict[str, _TransferCtx]:
    ctxs: dict[str, _TransferCtx] = {}
    for tr in transfers:
        lines = view.issue_lines(tr)
        ctx = _TransferCtx(transfer=tr, lines=lines)
        cursor = 0
        returned: list[Range] = []
        for line in lines:
            ctx.line_bounds.append(cursor)
            if line.outstanding_qty < 0 or line.outstanding_qty > line.qty:
                raise HandoverError(
                    f"调拨 {tr.doc_no!r} 的交付行 line_id={line.line_id} "
                    f"退回数量异常（qty={line.qty}, outstanding={line.outstanding_qty}）")
            if line.outstanding_qty < line.qty:
                returned.append((cursor + line.outstanding_qty,
                                 cursor + line.qty))
            cursor += line.qty
        ctx.line_bounds.append(cursor)
        ctx.ship_qty = cursor
        if cursor <= 0:
            raise HandoverError(f"调拨 {tr.doc_no!r} 的领料没有任何发货数量")
        ctx.returned = normalize(returned)
        ctx.signable = subtract([(0, cursor)], ctx.returned)
        ctxs[tr.doc_no] = ctx
    return ctxs


# ---------------------------------------------------------------------------
# 生效凭证 -> 认领
# ---------------------------------------------------------------------------
def _build_claims(view: _LedgerView, ctxs: dict[str, _TransferCtx],
                  effective: list[dict]) -> list[_Claim]:
    claims: list[_Claim] = []
    for rec in effective:
        content = rec["content"]
        if content[0] == "void":
            continue  # 作废版本不认领任何货
        (_, doc_no, ship_start, ship_end, wh, mat, seq, rec_start,
         length) = content
        where = (f"凭证 {rec['voucher_no']!r}（发布号 {rec['publish_no']}，"
                 f"{rec['where']}）")
        ctx = ctxs.get(doc_no)
        if ctx is None:
            raise HandoverError(f"{where} 引用了不存在的调拨号 {doc_no!r}")
        tr = ctx.transfer
        # 发货区段必须落在该票原始发货数量内，不裁剪越界数据
        if ship_end > ctx.ship_qty:
            raise HandoverError(
                f"{where} 的发货区段 [{ship_start}, {ship_end}) 越界："
                f"调拨 {doc_no!r} 原始发货数量为 {ctx.ship_qty}")
        if ship_start >= ship_end:
            raise HandoverError(f"{where} 的发货区段为空")
        receipt = Ref(wh, mat, seq)
        if wh != tr.dest_warehouse:
            raise HandoverError(
                f"{where} 的收料仓 {wh!r} 不是调拨 {doc_no!r} 的指定收仓 "
                f"{tr.dest_warehouse!r}")
        if mat != tr.issue.material:
            raise HandoverError(
                f"{where} 的收料物料 {mat!r} 与调拨 {doc_no!r} 的发货物料 "
                f"{tr.issue.material!r} 不同")
        receipt_qty = view.receipt_qty(receipt, where)
        if rec_start + length > receipt_qty:
            raise HandoverError(
                f"{where} 的收料区段 [{rec_start}, {rec_start + length}) 越界："
                f"收料 {receipt} 原始数量为 {receipt_qty}")
        claim = _Claim(voucher_no=rec["voucher_no"],
                       publish_no=rec["publish_no"], doc_no=doc_no,
                       ship_start=ship_start, ship_end=ship_end,
                       receipt=receipt, receipt_start=rec_start)
        # 已退料位置只在这些位置失效：保留每个未退料子区段的原始偏移
        for lo, hi in ctx.signable:
            a, b = max(lo, ship_start), min(hi, ship_end)
            if a < b:
                claim.intervals.append((a, b))
        claims.append(claim)
    return claims


# ---------------------------------------------------------------------------
# 收料侧认领清单（所有生效凭证的未退料部分）
# ---------------------------------------------------------------------------
def _receipt_occupancy(claims: list[_Claim]) -> dict[Ref, list[tuple[int, int, str]]]:
    occ: dict[Ref, list[tuple[int, int, str]]] = {}
    for c in claims:
        for lo, hi in c.intervals:
            rlo, rhi = c.receipt_range(lo, hi)
            occ.setdefault(c.receipt, []).append((rlo, rhi, c.voucher_no))
    for key, rows in occ.items():
        # 同一凭证可能有多段、指向同一收料；按区段起点稳定排序，便于输出
        occ[key] = sorted(rows, key=lambda r: (r[0], r[1], r[2]))
    return occ


def _receipt_claimants(occ, receipt: Ref, lo: int, hi: int) -> set[str]:
    out: set[str] = set()
    for a, b, voucher_no in occ.get(receipt, []):
        if overlap((a, b), (lo, hi)) > 0:
            out.add(voucher_no)
    return out


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------
def _segment_bounds(ctx: _TransferCtx,
                    occ: dict[Ref, list[tuple[int, int, str]]]) -> list[int]:
    qty = ctx.ship_qty
    bounds = {0, qty}
    bounds.update(ctx.line_bounds)
    for lo, hi in ctx.returned:
        bounds.add(lo)
        bounds.add(hi)
    for c in ctx.claims:
        for lo, hi in c.intervals:
            bounds.add(lo)
            bounds.add(hi)
            # 收料侧他人认领的边界映射回本票发货坐标：
            # 该处“其他认领凭证”集合可能变化，必须切开
            for a, b, voucher_no in occ.get(c.receipt, []):
                if voucher_no == c.voucher_no:
                    continue
                for rb in (a, b):
                    sb = rb - c.receipt_start + c.ship_start
                    if lo <= sb <= hi:
                        bounds.add(sb)
    return sorted(b for b in bounds if 0 <= b <= qty)


def _in_ranges(pos: int, ranges: list[Range]) -> bool:
    for lo, hi in ranges:
        if lo <= pos < hi:
            return True
    return False

def _claim_pieces(ctx: _TransferCtx, a: int, b: int):
    """覆盖发货段 [a, b) 的全部生效认领及其对应的收料子段。"""
    pieces = []
    for c in ctx.claims:
        for lo, hi in c.intervals:
            x, y = max(a, lo), min(b, hi)
            if x < y:
                pieces.append((c, x, y))
    return pieces


def _build_segments(ctx: _TransferCtx,
                    occ: dict[Ref, list[tuple[int, int, str]]]) -> list[Segment]:
    bounds = _segment_bounds(ctx, occ)
    segments: list[Segment] = []
    for a, b in zip(bounds, bounds[1:]):
        if a >= b:
            continue
        if _in_ranges(a, ctx.returned):
            segments.append(Segment(ship_start=a, ship_end=b, status=RETURNED))
            continue
        pieces = _claim_pieces(ctx, a, b)
        if not pieces:
            segments.append(Segment(ship_start=a, ship_end=b, status=PENDING))
            continue

        seg = Segment(ship_start=a, ship_end=b, status=PENDING)
        for c, x, y in pieces:
            # 收料侧子段：把该凭证区间内部的 [x, y) 映射到收料坐标
            rlo, rhi = c.receipt_range(x, y)
            # 同一凭证在相邻位置可能被同一个收料段连续覆盖；合并成一段
            if (seg.claims and seg.claims[-1].voucher_no == c.voucher_no
                    and seg.claims[-1].publish_no == c.publish_no
                    and seg.claims[-1].receipts[-1].receipt == c.receipt
                    and seg.claims[-1].receipts[-1].end == rlo):
                seg.claims[-1].ship_end = y
                seg.claims[-1].receipts[-1].end = rhi
                seg.claims[-1].unreturned_qty += y - x
            else:
                seg.claims.append(VoucherClaim(
                    voucher_no=c.voucher_no, publish_no=c.publish_no,
                    doc_no=c.doc_no, ship_start=x, ship_end=y,
                    receipts=[ReceiptSection(receipt=c.receipt,
                                             start=rlo, end=rhi)],
                    unreturned_qty=y - x))
            others = _receipt_claimants(occ, c.receipt, rlo, rhi)
            others.discard(c.voucher_no)
            seg.others.extend(sorted(others))

        distinct_ship = {c.voucher_no for c, _, _ in pieces}
        seg.others = sorted(set(seg.others))
        if len(distinct_ship) == 1 and not seg.others:
            seg.status = RECEIVED
            c = pieces[0][0]
            rlo, rhi = c.receipt_range(pieces[0][1], pieces[0][2])
            seg.source_receipt = c.receipt
            seg.source_start = rlo
            seg.source_end = rhi
        else:
            seg.status = DISPUTED
            seg.source_receipt = None
            seg.source_start = seg.source_end = None
            seg.conflict = _conflict_detail(pieces, occ)
        segments.append(seg)
    return segments


def _conflict_detail(pieces, occ) -> dict:
    """争议段的两侧坐标：发货侧重叠位置与收料侧重叠位置。

    发货侧只列本票参与该段的凭证；收料侧列该收料段上的全部认领凭证
    （含其他调拨的），便于先核对两边的重叠坐标、再决定改哪一份凭证。
    """
    ship_vouchers: dict[str, Range] = {}
    receipt_ranges: dict[Ref, Range] = {}
    for c, x, y in pieces:
        if c.voucher_no not in ship_vouchers:
            ship_vouchers[c.voucher_no] = (x, y)
        else:
            lo, hi = ship_vouchers[c.voucher_no]
            ship_vouchers[c.voucher_no] = (min(lo, x), max(hi, y))
        rlo, rhi = c.receipt_range(x, y)
        cur = receipt_ranges.get(c.receipt)
        receipt_ranges[c.receipt] = ((rlo, rhi) if cur is None else
                                     (min(cur[0], rlo), max(cur[1], rhi)))
    receipts_json = []
    for receipt, (lo, hi) in sorted(receipt_ranges.items(),
                                    key=lambda kv: kv[0].sort_key()):
        claimants = {v for a, b, v in occ.get(receipt, [])
                     if overlap((a, b), (lo, hi)) > 0}
        receipts_json.append({"receipt": receipt.as_json(),
                              "start": lo, "end": hi,
                              "vouchers": sorted(claimants)})
    return {
        "ship": {
            "start": min(r[0] for r in ship_vouchers.values()),
            "end": max(r[1] for r in ship_vouchers.values()),
            "vouchers": sorted(ship_vouchers),
        },
        "receipts": receipts_json,
    }


# ---------------------------------------------------------------------------
# 顶层
# ---------------------------------------------------------------------------
def analyze(run_result, transfers: list[Transfer], records: list[dict],
            limit: int) -> ReconciliationReport:
    broken = [g for g in run_result.groups if g.error]
    if broken:
        details = "; ".join(
            f"{g.warehouse}/{g.material} seq={g.error_seq}: {g.error}"
            for g in broken)
        raise HandoverError(f"仓内流水存在断点：{details}")

    view = _LedgerView(run_result)
    ctxs = _build_contexts(view, transfers)
    effective = resolve_versions(records, limit)
    claims = _build_claims(view, ctxs, effective)

    # 认领按调拨归位；同一凭证号只属于一票
    for c in claims:
        ctxs[c.doc_no].claims.append(c)
    occ = _receipt_occupancy(claims)

    report = ReconciliationReport(publish_limit=limit)
    for doc_no in sorted(ctxs):
        ctx = ctxs[doc_no]
        ctx.claims.sort(key=lambda c: (c.ship_start, c.ship_end,
                                       c.voucher_no, c.publish_no))
        report.transfers.append(TransferReport(
            doc_no=doc_no, issue=ctx.transfer.issue,
            dest_warehouse=ctx.transfer.dest_warehouse,
            ship_qty=ctx.ship_qty,
            segments=_build_segments(ctx, occ)))
    report.transfers.sort(key=lambda t: (t.doc_no, t.issue.sort_key()))
    return report


def run_handover(flow_path: str, handover_path: str, limit: int) -> ReconciliationReport:
    try:
        actions = parse_file(flow_path)
    except (InputError, OSError, ValueError) as exc:
        raise HandoverError(f"仓内流水无法解析：{exc}")
    transfers, records = load_handover(handover_path)
    return analyze(run(actions), transfers, records, limit)
