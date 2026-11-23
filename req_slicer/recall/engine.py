"""召回影响分析引擎（离线、纯标准库）。

输入：仓内流水（复用 req_slicer.engine 的解释结果）、生产单清单、召回请求。
输出：受影响生产收料的冻结区段清单；任何断点/引用缺失/数量不符/成环，
      都以 RecallError 失败，绝不输出看似完整的冻结清单。

追踪语义
========
1. 上游：以“账期结束时仍由该笔领料占用的原料段”为准。已退回的尾部不再
   算给原领料；退回后又被另一笔领料取走，则随那笔领料的归属。领料引用与
   收料引用都只按 (仓, 物料, 动作序号) 定位。
2. 保序加工：把该单各领料的实际投料区段按清单顺序（以及领料内原交付行
   顺序）连成一条连续坐标轴，再按产出顺序切给各笔收料。召回区段在坐标轴
   上取交，跨产出边界时只冻结各自相交的部分。投入与产出总量必须相等。
3. 混料：任一实际投料与召回覆盖相交，就冻结该单全部产出区段（不要求
   投入与产出数量相等）。
4. 中间品：再次领用时只传递实际取到的召回区段。召回区段按“哪一笔上游
   收料”分开保存；同一段被多条来路指向时先合并再计量，沿路径不累加。
5. 每条领料最多归一个生产单；每工单实际投料总量必须大于零（单笔领料
   可以全部退回，但不能让整单没有实际投料）。
6. 不同仓或物料的动作序号不能用来推断工序先后：先后只来自生产单里显式
   写出的“领料 -> 产出”引用关系。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..engine.model import MaterialLedger
from ..engine.runner import RunResult
from .inputs import parse_production, parse_recall_request
from .model import (RecallError, Ref, IssueNode, ReceiptNode, WorkOrder,
                    Range, normalize, intersect, total)


@dataclass
class AffectedReceipt:
    """一笔受影响的生产收料：冻结区段以该收料自身原料段坐标表达。"""

    doc_no: str
    ref: Ref
    segments: list[Range] = field(default_factory=list)

    @property
    def qty(self) -> int:
        return total(self.segments)

    def as_json(self) -> dict:
        return {
            "receipt": self.ref.as_json(),
            "work_order": self.doc_no,
            "segments": [{"start": s, "end": e, "qty": e - s}
                         for s, e in self.segments],
            "qty_total": self.qty,
        }


@dataclass
class RecallReport:
    affected: list[AffectedReceipt] = field(default_factory=list)

    def as_json(self) -> dict:
        return {"affected_receipts": [a.as_json() for a in self.affected]}


# ---------------------------------------------------------------------------
# 台账查找：从既有解释结果里按 (仓, 物料, 动作序号) 取真实动作
# ---------------------------------------------------------------------------
class _LedgerView:
    def __init__(self, run_result: RunResult):
        self.ledgers: dict[tuple[str, str], MaterialLedger] = {
            (g.warehouse, g.material): g.ledger for g in run_result.groups}

    def ledger(self, ref: Ref) -> MaterialLedger:
        lg = self.ledgers.get((ref.warehouse, ref.material))
        if lg is None:
            raise RecallError(
                f"流水里没有 ({ref.warehouse}, {ref.material}) 这个仓/物料")
        return lg

    def issue_doc(self, ref: Ref) -> str:
        doc = self.ledger(ref).issue_docs.get(ref.action_seq)
        if doc is None:
            raise RecallError(
                f"领料引用 {ref} 在流水中不存在（该序号不是领料动作）")
        return doc

    def issue_lines(self, ref: Ref) -> list:
        doc = self.issue_doc(ref)
        return [l for l in self.ledger(ref).lines
                if l.issue_seq == ref.action_seq and l.doc_no == doc]

    def issue_outstanding(self, ref: Ref) -> list[tuple[int, int, int]]:
        """期末仍由该笔领料占用的原料段：[(receipt_seq, start, end)]，按原交付行顺序。

        已退回的尾部以交付行的 outstanding_qty 表达，天然排除；若退回的段后来
        被另一笔领料取走，它会出现在那笔领料的交付行里，跟随新的归属。
        """
        self.issue_doc(ref)  # 引用不存在即抛错
        out: list[tuple[int, int, int]] = []
        for line in self.issue_lines(ref):
            if line.outstanding_qty <= 0:
                continue
            out.append((line.receipt_seq, line.seg_start,
                        line.seg_start + line.outstanding_qty))
        return out

    def receipt_original(self, ref: Ref):
        sl = self.ledger(ref).receipts.get(ref.action_seq)
        if sl is None:
            raise RecallError(f"收料引用 {ref} 在流水中不存在（该序号不是收料动作）")
        return sl


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------
def analyze(run_result: RunResult, work_orders: list[WorkOrder],
            recall_receipt: Ref, recall_segments: list[Range]) -> RecallReport:
    broken = [g for g in run_result.groups if g.error]
    if broken:
        details = "; ".join(
            f"{g.warehouse}/{g.material} seq={g.error_seq}: {g.error}"
            for g in broken)
        raise RecallError(f"仓内流水存在断点：{details}")

    view = _LedgerView(run_result)

    # 区段先合并：相交/相邻的请求区段表示同一份覆盖，不重复计量
    recall_segments = normalize(recall_segments)
    if not recall_segments:
        raise RecallError("召回请求没有任何非空区段")

    # ---- 1) 召回请求本身：必须是真实收料、外部到货，且区段不越界 ----
    original = view.receipt_original(recall_receipt)
    for start, end in recall_segments:
        if start < 0 or end > original.seg_end:
            raise RecallError(
                f"召回区段 [{start}, {end}) 越界：收料 {recall_receipt} 只有 "
                f"[0, {original.seg_end})")
    declared_receipts = {ref for wo in work_orders for ref in wo.receipts}
    if recall_receipt in declared_receipts:
        raise RecallError(
            f"召回请求指定的收料 {recall_receipt} 是生产单产出，不是外部到货")

    # ---- 2) 清点引用（存在性与“领料/收料”身份）----
    issue_index: dict[Ref, IssueNode] = {}
    receipt_index: dict[Ref, ReceiptNode] = {}
    for wo in work_orders:
        for ref in wo.issues:
            issue_index[ref] = IssueNode(ref=ref, doc_no=view.issue_doc(ref))
        for ref in wo.receipts:
            node = ReceiptNode(ref=ref)
            node.full_length = view.receipt_original(ref).seg_end
            node.work_order = wo
            receipt_index[ref] = node

    order_of = {wo.doc_no: i for i, wo in enumerate(work_orders)}

    # ---- 3) 工序依赖：领料实际取到的上游收料属于哪张生产单 ----
    # 依赖是生产清单声明的结构关系：只要某条领料引用取到过某产出收料，
    # 该单就依赖那笔产出的生产单。退回与否不改变声明的先后关系，成环必须失败。
    upstream: dict[str, set[str]] = {wo.doc_no: set() for wo in work_orders}
    for wo in work_orders:
        for ref in wo.issues:
            for line in view.issue_lines(ref):
                node = receipt_index.get(
                    Ref(ref.warehouse, ref.material, line.receipt_seq))
                if node is not None:
                    upstream[wo.doc_no].add(node.work_order.doc_no)
    order = _dependency_order(upstream, order_of)

    # ---- 4) 外部到货收料：只有它本身被召回时才成为种子 ----
    seed = ReceiptNode(ref=recall_receipt, external=True)
    seed.segments = list(recall_segments)
    receipt_index[recall_receipt] = seed

    # ---- 5) 按依赖顺序处理，上游产出先就绪 ----
    by_doc = {wo.doc_no: wo for wo in work_orders}
    for doc in order:
        _process_work_order(view, by_doc[doc], issue_index, receipt_index)

    # ---- 6) 汇总受影响的生产收料 ----
    affected: list[AffectedReceipt] = []
    for wo in work_orders:
        for ref in wo.receipts:
            node = receipt_index[ref]
            if node.external or not node.segments:
                continue
            affected.append(AffectedReceipt(
                doc_no=wo.doc_no, ref=ref,
                segments=normalize(node.segments)))
    affected.sort(key=lambda a: (a.ref.sort_key(), a.doc_no))
    return RecallReport(affected=affected)


def _dependency_order(upstream: dict[str, set[str]],
                      order_of: dict[str, int]) -> list[str]:
    """Kahn 拓扑排序：上游先处理；同层按生产单声明顺序，结果稳定。

    仍有剩余节点即成环，明确报错；不递归，长链也不触发栈深限制。
    """
    pending = {doc: set(parents) for doc, parents in upstream.items()}
    done: set[str] = set()
    order: list[str] = []
    while len(done) < len(pending):
        ready = sorted((doc for doc, parents in pending.items()
                        if doc not in done and parents <= done),
                       key=lambda d: order_of[d])
        if not ready:
            cycle = sorted((doc for doc in pending if doc not in done),
                           key=lambda d: order_of[d])
            raise RecallError("工序依赖成环：" + " -> ".join(cycle))
        for doc in ready:
            done.add(doc)
            order.append(doc)
    return order


def _upstream_segments(view: _LedgerView, ref: Ref,
                       receipt_index: dict[Ref, ReceiptNode]) -> dict[int, list[Range]]:
    """一笔领料实际取到的召回区段，按上游收料序号分组（各自原料段坐标）。

    只看该领料期末仍占用的部分；与上游收料被召回的区段取交。同一收料序号
    内多条来路指向同一段时先合并，不重复计量。
    """
    raw: dict[int, list[Range]] = {}
    for receipt_seq, seg_start, seg_end in view.issue_outstanding(ref):
        up = receipt_index.get(Ref(ref.warehouse, ref.material, receipt_seq))
        if up is None or not up.segments:
            continue
        hits = intersect([(seg_start, seg_end)], up.segments)
        if hits:
            raw.setdefault(receipt_seq, []).extend(hits)
    return {seq: normalize(ranges) for seq, ranges in raw.items()}


def _process_work_order(view: _LedgerView, wo: WorkOrder,
                        issue_index: dict[Ref, IssueNode],
                        receipt_index: dict[Ref, ReceiptNode]) -> None:
    # 先在每条领料上算出“实际取到的召回区段”，这是中间品向下游传递的唯一依据
    for ref in wo.issues:
        issue_index[ref].segments = _upstream_segments(view, ref, receipt_index)

    input_total = sum(total([(s, e) for _, s, e in view.issue_outstanding(ref)])
                      for ref in wo.issues)
    if input_total <= 0:
        raise RecallError(
            f"生产单 {wo.doc_no} 无实际投料（各领料引用期末仍占用量合计为 0）")

    if wo.process == "ordered":
        _process_ordered(view, wo, issue_index, receipt_index, input_total)
    else:
        _process_mixed(wo, issue_index, receipt_index)


def _process_ordered(view: _LedgerView, wo: WorkOrder,
                     issue_index: dict[Ref, IssueNode],
                     receipt_index: dict[Ref, ReceiptNode],
                     input_total: int) -> None:
    # 产出总量：各笔收料原料段长度按产出顺序排布
    output_spec: list[tuple[Ref, int, int]] = []  # (ref, base, length)
    output_total = 0
    for ref in wo.receipts:
        length = view.receipt_original(ref).seg_end
        output_spec.append((ref, output_total, length))
        output_total += length
    if input_total != output_total:
        raise RecallError(
            f"生产单 {wo.doc_no} 保序加工投入/产出数量不符："
            f"投入 {input_total}，产出 {output_total}")

    # 把各领料的实际投料按清单顺序连成一条连续坐标轴，并把召回区段映射上去
    recall_axis: list[Range] = []
    cursor = 0
    for ref in wo.issues:
        node = issue_index[ref]
        for receipt_seq, seg_start, seg_end in view.issue_outstanding(ref):
            # 该子片内的召回区段：原料段坐标按子片在轴上的位置平移
            for lo, hi in intersect([(seg_start, seg_end)],
                                    node.segments.get(receipt_seq, [])):
                recall_axis.append((cursor + (lo - seg_start),
                                    cursor + (hi - seg_start)))
            cursor += seg_end - seg_start
    recall_axis = normalize(recall_axis)

    # 按产出顺序切轴：每笔收料只冻结与自己区间相交的部分
    for ref, base, length in output_spec:
        node = receipt_index[ref]
        within = intersect(recall_axis, [(base, base + length)])
        segs = [(lo - base, hi - base) for lo, hi in within]
        node.segments = normalize(node.segments + segs)


def _process_mixed(wo: WorkOrder, issue_index: dict[Ref, IssueNode],
                   receipt_index: dict[Ref, ReceiptNode]) -> None:
    # 混料不再区分投入位置：任一实际投料有召回覆盖，整单全部产出被冻结
    if not any(issue_index[ref].segments for ref in wo.issues):
        return
    for ref in wo.receipts:
        node = receipt_index[ref]
        node.segments = [(0, node.full_length)]


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------
def run_recall(flow_path: str, production_path: str, request_path: str) -> RecallReport:
    from ..engine.inputs import parse_file, InputError
    from ..engine.runner import run

    try:
        actions = parse_file(flow_path)
    except InputError as exc:
        raise RecallError(f"仓内流水无法解析：{exc}")
    work_orders = parse_production(production_path)
    receipt, segments = parse_recall_request(request_path)
    return analyze(run(actions), work_orders, receipt, segments)
