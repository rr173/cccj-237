"""召回影响分析测试：双支路汇合的精确坐标、退料前后混料对比、失败模式。

运行（仓库根目录）::

    python3 -I -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from req_slicer.engine.inputs import parse_file
from req_slicer.engine.runner import run
from req_slicer.recall import cli as recall_cli
from req_slicer.recall.engine import analyze, run_recall
from req_slicer.recall.inputs import parse_production, parse_recall_request
from req_slicer.recall.model import RecallError, Ref, normalize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")

FLOW = os.path.join(SAMPLES, "recall_flow.json")
FLOW_BEFORE_RETURN = os.path.join(SAMPLES, "recall_flow_before_return.json")
PRODUCTION = os.path.join(SAMPLES, "recall_production.json")
REQUEST_AB = os.path.join(SAMPLES, "recall_request_ab.json")


def _write(dirname, name, payload):
    path = os.path.join(dirname, name)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh, ensure_ascii=False)
    return path


def _receipts(report):
    """把报告压成便于断言的形状：[(生产单, 物料, [(start, end)], 合计)]。"""
    return [(a.doc_no, a.ref.material, a.segments, a.qty)
            for a in report.affected]


def _flow(*actions):
    return [dict(a) for a in actions]


class TestBranchMergeWorkedExample(unittest.TestCase):
    """两条加工支路汇合：WO-A / WO-B -> WO-F -> WO-D1、WO-D2。

    召回 RM 的 [20, 40)（RA[0,40) 在交付行 I-A 里，对应 WO-A 的第 20..40 件）。
    """

    @classmethod
    def setUpClass(cls):
        cls.flow = run(parse_file(FLOW))
        cls.work_orders = parse_production(PRODUCTION)

    def _analyze(self, segments, flow=None):
        return analyze(flow or self.flow, self.work_orders,
                       Ref("WH-A", "RM", 1), normalize(segments))

    def test_partial_receipt_exact_coordinates(self):
        report = self._analyze([(20, 40)])
        # WO-A 的 MA 只有 [20,40) 受影响；WO-F 的 MP 对应 [20,40)；
        # WO-D2 唯一取到 MP[20,40) 的那一段，映射为 ME[0,20)。
        self.assertEqual(_receipts(report), [
            ("WO-A", "MA", [(20, 40)], 20),
            ("WO-F", "MP", [(20, 40)], 20),
            ("WO-D2", "ME", [(0, 20)], 20),
        ])

    def test_clean_downstream_not_frozen(self):
        # WO-D1 只取 WO-F 产出的 MP[0,20)，这段来自未被召回的 MA[0,20)；
        # 它必须完全不出现在清单里。
        affected_docs = {a.doc_no for a in self._analyze([(20, 40)]).affected}
        self.assertNotIn("WO-D1", affected_docs)
        self.assertNotIn("WO-B", affected_docs)
        self.assertNotIn("WO-M", affected_docs)

    def test_downstream_taking_only_clean_part_not_frozen(self):
        # WO-D2 取的是 MP[20,70)：只有 MP[20,60) 的来料受召回影响，
        # 因此冻结 [0,40) 而不是整笔 ME；WO-D1 取的 MP[0,20) 全干净。
        report = self._analyze([(20, 40)])
        by_doc = {a.doc_no: a for a in report.affected}
        self.assertIn("WO-D2", by_doc)
        self.assertEqual(by_doc["WO-D2"].segments, [(0, 20)])
        self.assertLess(by_doc["WO-D2"].qty, 50)   # 收料全长 50，仅局部
        self.assertNotIn("WO-D1", by_doc)

    def test_adjacent_request_ranges_merge_and_are_not_double_counted(self):
        split = self._analyze([(20, 25), (25, 40)])
        whole = self._analyze([(20, 40)])
        self.assertEqual(_receipts(split), _receipts(whole))

        # [20,40) 与 [40,60) 相邻，合并为 [20,60)，跨两条支路与两笔产出
        merged = self._analyze([(20, 40), (40, 60)])
        self.assertEqual(_receipts(merged), [
            ("WO-A", "MA", [(20, 40)], 20),
            ("WO-B", "MC", [(0, 20)], 20),
            ("WO-F", "MP", [(20, 60)], 40),
            ("WO-D2", "ME", [(0, 40)], 40),
        ])
        # 数量合计为 40，不是把两条请求区段各算一次得到 60
        self.assertEqual(sum(a.qty for a in merged.affected
                             if a.doc_no == "WO-F"), 40)

    def test_recall_crosses_output_boundary_freezes_each_intersection(self):
        # WH-Z/RM[25,40)：投料轴 [25,50) 先排 P-E1[0,25) 再排 P-E2[0,25)，
        # 召回跨过两笔产出的边界，只冻结 [25,40) 与各自的相交部分。
        actions = run(parse_file(FLOW))
        report = analyze(actions, self.work_orders, Ref("WH-Z", "RM", 17),
                         normalize([(25, 40)]))
        self.assertEqual(_receipts(report), [
            ("WO-E", "MA", [(0, 15)], 15),
        ])
        # 同一产出另一侧：RM[15,25) 全落在第一笔产出区间内；下游再领走的
        # MA[15,25) 恰好就是这 10 件，因此 WO-HIT 也被局部冻结。
        left = analyze(actions, self.work_orders, Ref("WH-Z", "RM", 17),
                       normalize([(15, 25)]))
        self.assertEqual(_receipts(left), [
            ("WO-HIT", "MQ2", [(0, 10)], 10),   # 下游产品，逐字典序在前
            ("WO-E", "MA", [(15, 25)], 10),
        ])

    def test_leaves_recall_spans_two_outputs_with_exact_coordinates(self):
        flow = run(parse_file(FLOW))
        report = analyze(flow, self.work_orders, Ref("WH-Z", "RM", 17),
                         normalize([(15, 40)]))
        # 收料按 (仓, 物料, 序号) 排序：WH-Y 的 MQ2 在 WH-Z 的 MA 之前
        self.assertEqual(_receipts(report), [
            ("WO-HIT", "MQ2", [(0, 10)], 10),
            ("WO-E", "MA", [(15, 25)], 10),   # 第一笔产出：轴 15..25
            ("WO-E", "MA", [(0, 15)], 15),    # 第二笔产出：轴 25..40
        ])

    def test_later_order_reading_only_clean_leaf_not_frozen(self):
        # 同一产出 P-E1 被两笔下游领料分走：MA[0,15) 干净、MA[15,25) 命中。
        # 只取干净部分的 WO-CLEAN 不得出现；取到命中部分的 WO-HIT 才被冻结。
        flow = run(parse_file(FLOW))
        report = analyze(flow, self.work_orders, Ref("WH-Z", "RM", 17),
                         normalize([(15, 25)]))
        docs = {a.doc_no for a in report.affected}
        self.assertIn("WO-HIT", docs)
        self.assertNotIn("WO-CLEAN", docs)
        clean = next(a for a in report.affected if a.doc_no == "WO-HIT")
        self.assertEqual(clean.segments, [(0, 10)])
        self.assertEqual(clean.qty, 10)          # 收料全长 10，整笔命中
        # 另一条只落在干净区段内的召回同样不能误伤 WO-CLEAN
        clean_only = analyze(flow, self.work_orders, Ref("WH-Z", "RM", 17),
                             normalize([(20, 25)]))
        self.assertNotIn("WO-CLEAN", {a.doc_no for a in clean_only.affected})
        # 对照：召回上半段 RM[0,15) 时，MA[0,15) 本身受污染，
        # 取这段的 WO-CLEAN 必须出现（不能把“未命中”误判成“已命中”的反面）。
        lower_half = analyze(flow, self.work_orders, Ref("WH-Z", "RM", 17),
                             normalize([(0, 15)]))
        docs = {a.doc_no for a in lower_half.affected}
        self.assertEqual(docs, {"WO-E", "WO-CLEAN"})
        self.assertNotIn("WO-HIT", docs)

    def test_full_batch_recall(self):
        report = self._analyze([(0, 120)])
        self.assertEqual(_receipts(report), [
            ("WO-A", "MA", [(0, 40)], 40),
            ("WO-B", "MC", [(0, 30)], 30),
            ("WO-F", "MP", [(0, 70)], 70),
            ("WO-D1", "MD", [(0, 20)], 20),
            ("WO-D2", "ME", [(0, 50)], 50),
            ("WO-M", "MM", [(0, 60)], 60),
        ])
        # 另一仓的另一笔外部到货整批召回，与上例互不影响
        huge = analyze(self.flow, self.work_orders, Ref("WH-Z", "RM", 17),
                       normalize([(0, 50)]))
        self.assertEqual(_receipts(huge), [
            ("WO-CLEAN", "MQ", [(0, 15)], 15),
            ("WO-HIT", "MQ2", [(0, 10)], 10),
            ("WO-E", "MA", [(0, 25)], 25),
            ("WO-E", "MA", [(0, 25)], 25),
        ])
        self.assertEqual({a.ref.action_seq for a in huge.affected}, {19, 20, 23, 24})

    def test_output_is_sorted_merged_and_deterministic(self):
        first = self._analyze([(60, 80), (20, 40)]).as_json()
        second = self._analyze([(20, 40), (60, 80)]).as_json()
        self.assertEqual(first, second)
        # 顶层生产单顺序不影响结果
        reordered = parse_production(PRODUCTION)[::-1]
        third = analyze(self.flow, reordered, Ref("WH-A", "RM", 1),
                        normalize([(20, 40), (60, 80)])).as_json()
        self.assertEqual(first, third)
        for entry in first["affected_receipts"]:
            starts = [s["start"] for s in entry["segments"]]
            self.assertEqual(starts, sorted(starts))
            for a, b in zip(entry["segments"], entry["segments"][1:]):
                self.assertLess(a["end"], b["start"])  # 不重叠且不相邻

    def test_rerun_from_files_is_identical(self):
        one = run_recall(FLOW, PRODUCTION, REQUEST_AB).as_json()
        two = run_recall(FLOW, PRODUCTION, REQUEST_AB).as_json()
        self.assertEqual(one, two)


class TestReturnBeforeAfterMixed(unittest.TestCase):
    """同一条召回通知，比较退料前后的混料结果。"""

    @classmethod
    def setUpClass(cls):
        cls.before = run(parse_file(FLOW_BEFORE_RETURN))
        cls.after = run(parse_file(FLOW))
        cls.work_orders = parse_production(PRODUCTION)

    def _docs(self, flow, segments):
        report = analyze(flow, self.work_orders, Ref("WH-A", "RM", 1),
                         normalize(segments))
        return {a.doc_no for a in report.affected}, report

    def test_same_notice_before_and_after_return(self):
        # I-D 退料前占用 RM[100,120)，退 15 后只占用 [100,105)，
        # [105,120) 已退回在库、未被别的领料取走。
        before_docs, before = self._docs(self.before, [(105, 120)])
        self.assertIn("WO-M", before_docs)
        self.assertEqual(_receipts(before), [("WO-M", "MM", [(0, 60)], 60)])

        after_docs, after = self._docs(self.after, [(105, 120)])
        self.assertNotIn("WO-M", after_docs)
        self.assertEqual(after.as_json(), {"affected_receipts": []})

        # 只落在仍被占用的 [100,105) 内：退料前后同样命中混料工单
        for flow in (self.before, self.after):
            docs, report = self._docs(flow, [(100, 105)])
            self.assertIn("WO-M", docs)
            self.assertEqual(_receipts(report), [("WO-M", "MM", [(0, 60)], 60)])

    def test_returned_tail_not_attributed_to_original_issue(self):
        # 保序单里也一致：I-D 退回的尾部不能算作原领料的实际投料
        docs, _ = self._docs(self.after, [(105, 120)])
        self.assertEqual(docs, set())

    def test_reissued_segment_follows_new_issue(self):
        # 退回后由另一笔领料重新取走，归属跟随新领料：用一份临时流水验证。
        actions = _flow(
            {"seq": 1, "action_id": "R1", "op": "receive", "material": "RM",
             "warehouse": "WH-A", "batch": "RA",
             "age_start": "2026-01-01", "qty": 20, "location": "A"},
            {"seq": 2, "action_id": "J1", "op": "issue", "material": "RM",
             "warehouse": "WH-A", "ref_doc": "J1", "qty": 20},
            {"seq": 3, "action_id": "T1", "op": "return", "material": "RM",
             "warehouse": "WH-A", "ref_doc": "J1", "qty": 10, "location": "A"},
            {"seq": 4, "action_id": "J2", "op": "issue", "material": "RM",
             "warehouse": "WH-A", "ref_doc": "J2", "qty": 10},
            {"seq": 5, "action_id": "P1", "op": "receive", "material": "MA",
             "warehouse": "WH-A", "batch": "PA",
             "age_start": "2026-01-02", "qty": 10, "location": "A"},
            {"seq": 6, "action_id": "P2", "op": "receive", "material": "MB",
             "warehouse": "WH-B", "batch": "PB",
             "age_start": "2026-01-03", "qty": 10, "location": "B"},
        )
        flow = run([_action(a) for a in actions])
        work_orders = parse_production_obj([
            {"doc_no": "WO-OLD", "process": "ordered",
             "issues": [{"warehouse": "WH-A", "material": "RM",
                         "action_seq": 2}],
             "receipts": [{"warehouse": "WH-A", "material": "MA",
                           "action_seq": 5}]},
            {"doc_no": "WO-NEW", "process": "ordered",
             "issues": [{"warehouse": "WH-A", "material": "RM",
                         "action_seq": 4}],
             "receipts": [{"warehouse": "WH-B", "material": "MB",
                           "action_seq": 6}]},
        ])
        # J1 期末只占用 RM[0,10)，J2 占用退回的 RM[10,20)
        old = analyze(flow, work_orders, Ref("WH-A", "RM", 1),
                      normalize([(15, 20)]))
        self.assertEqual(_receipts(old), [("WO-NEW", "MB", [(5, 10)], 5)])
        retained = analyze(flow, work_orders, Ref("WH-A", "RM", 1),
                           normalize([(5, 10)]))
        self.assertEqual(_receipts(retained), [("WO-OLD", "MA", [(5, 10)], 5)])


def _action(raw):
    from req_slicer.engine.model import Action
    return Action(seq=raw["seq"], action_id=raw["action_id"], op=raw["op"],
                  material=raw["material"], warehouse=raw["warehouse"],
                  batch=raw.get("batch"), age_start=raw.get("age_start"),
                  qty=raw.get("qty"), location=raw.get("location"),
                  from_location=raw.get("from_location"),
                  isolate_id=raw.get("isolate_id"), ref_doc=raw.get("ref_doc"))


def parse_production_obj(orders):
    with tempfile.TemporaryDirectory() as td:
        path = _write(td, "prod.json", {"work_orders": orders})
        return parse_production(path)


class TestInputValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flow = run(parse_file(FLOW))
        cls.work_orders = parse_production(PRODUCTION)

    def _expect(self, needle, *, flow=None, work_orders=None, segments=None,
                receipt=None):
        with self.assertRaises(RecallError) as ctx:
            analyze(flow or self.flow,
                    work_orders if work_orders is not None else self.work_orders,
                    receipt or Ref("WH-A", "RM", 1),
                    normalize(segments if segments is not None else [(20, 40)]))
        self.assertIn(needle, str(ctx.exception))

    def test_flow_breakpoint_is_fatal(self):
        broken = run(parse_file(os.path.join(SAMPLES, "flow_breakpoints.json")))
        self._expect("流水存在断点", flow=broken)

    def test_missing_issue_reference(self):
        ref = Ref("WH-C", "MP", 999)
        order = parse_production_obj([{
            "doc_no": "WO-X", "process": "ordered",
            "issues": [ref.as_json()],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 6}],
        }])[0]
        self._expect("领料引用", work_orders=[order])

    def test_missing_receipt_reference(self):
        order = parse_production_obj([{
            "doc_no": "WO-X", "process": "ordered",
            "issues": [{"warehouse": "WH-A", "material": "RM",
                        "action_seq": 2}],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 999}],
        }])[0]
        self._expect("收料引用", work_orders=[order])

    def test_reference_type_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "prod.json", {"work_orders": [{
                "doc_no": "WO-X", "process": "ordered",
                "issues": [{"warehouse": "WH-A", "material": "RM",
                            "action_seq": "2"}],
                "receipts": [{"warehouse": "WH-A", "material": "MA",
                              "action_seq": 6}],
            }]})
            with self.assertRaisesRegex(RecallError, "必须是整数"):
                parse_production(path)

    def test_unknown_process_type(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "prod.json", {"work_orders": [{
                "doc_no": "WO-X", "process": "assembly",
                "issues": [{"warehouse": "WH-A", "material": "RM",
                            "action_seq": 2}],
                "receipts": [{"warehouse": "WH-A", "material": "MA",
                              "action_seq": 6}],
            }]})
            with self.assertRaisesRegex(RecallError, "process"):
                parse_production(path)

    def test_duplicate_issue_ownership(self):
        ref = {"warehouse": "WH-A", "material": "RM", "action_seq": 2}
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "prod.json", {"work_orders": [
                {"doc_no": "WO-X", "process": "ordered", "issues": [ref],
                 "receipts": [{"warehouse": "WH-A", "material": "MA",
                               "action_seq": 6}]},
                {"doc_no": "WO-Y", "process": "ordered", "issues": [ref],
                 "receipts": [{"warehouse": "WH-A", "material": "MA",
                               "action_seq": 6}]},
            ]})
            with self.assertRaisesRegex(RecallError, "同时被生产单"):
                parse_production(path)

    def test_duplicate_receipt_declaration(self):
        ref = {"warehouse": "WH-A", "material": "MA", "action_seq": 6}
        issue = {"warehouse": "WH-A", "material": "RM", "action_seq": 2}
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "prod.json", {"work_orders": [
                {"doc_no": "WO-X", "process": "ordered", "issues": [issue],
                 "receipts": [ref]},
                {"doc_no": "WO-Y", "process": "ordered",
                 "issues": [{"warehouse": "WH-A", "material": "RM",
                             "action_seq": 3}],
                 "receipts": [ref]},
            ]})
            with self.assertRaisesRegex(RecallError, "声明为产出"):
                parse_production(path)

    def test_recall_request_out_of_bounds(self):
        self._expect("越界", segments=[(0, 121)])

    def test_recall_request_on_production_output(self):
        self._expect("不是外部到货", segments=[(0, 10)],
                     receipt=Ref("WH-A", "MA", 6))

    def test_recall_request_type_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "req.json", {
                "receipt": {"warehouse": "WH-A", "material": "RM",
                            "action_seq": 1},
                "segments": [[0, 10.5]],
            })
            with self.assertRaisesRegex(RecallError, "必须是整数"):
                parse_recall_request(path)

    def test_recall_request_empty_or_inverted_segment(self):
        for segments in ([], [[5, 5]], [[6, 5]]):
            with tempfile.TemporaryDirectory() as td:
                path = _write(td, "req.json", {
                    "receipt": {"warehouse": "WH-A", "material": "RM",
                                "action_seq": 1},
                    "segments": segments,
                })
                with self.assertRaises(RecallError):
                    parse_recall_request(path)

    def test_no_actual_input_after_full_return(self):
        actions = [_action({
            "seq": 1, "action_id": "R1", "op": "receive", "material": "RM",
            "warehouse": "WH-A", "batch": "RA", "age_start": "2026-01-01",
            "qty": 10, "location": "A"}),
            _action({"seq": 2, "action_id": "J1", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J1", "qty": 10}),
            _action({"seq": 3, "action_id": "T1", "op": "return",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J1", "qty": 10, "location": "A"}),
            _action({"seq": 4, "action_id": "P1", "op": "receive",
                     "material": "MA", "warehouse": "WH-A", "batch": "PA",
                     "age_start": "2026-01-02", "qty": 10, "location": "A"})]
        flow = run(actions)
        order = parse_production_obj([{
            "doc_no": "WO-X", "process": "mixed",
            "issues": [{"warehouse": "WH-A", "material": "RM",
                        "action_seq": 2}],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 4}],
        }])[0]
        with self.assertRaisesRegex(RecallError, "无实际投料"):
            analyze(flow, [order], Ref("WH-A", "RM", 1), normalize([(0, 10)]))

    def test_one_issue_fully_returned_is_allowed_if_other_issue_remains(self):
        # 同一工单两笔领料，其中一笔全部退回：只要整单实际投料 > 0 即可。
        actions = [_action({
            "seq": 1, "action_id": "R1", "op": "receive", "material": "RM",
            "warehouse": "WH-A", "batch": "RA", "age_start": "2026-01-01",
            "qty": 20, "location": "A"}),
            _action({"seq": 2, "action_id": "J1", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J1", "qty": 10}),
            _action({"seq": 3, "action_id": "J2", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J2", "qty": 10}),
            _action({"seq": 4, "action_id": "T2", "op": "return",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J2", "qty": 10, "location": "A"}),
            _action({"seq": 5, "action_id": "P1", "op": "receive",
                     "material": "MA", "warehouse": "WH-A", "batch": "PA",
                     "age_start": "2026-01-02", "qty": 10, "location": "A"})]
        flow = run(actions)
        order = parse_production_obj([{
            "doc_no": "WO-X", "process": "ordered",
            "issues": [{"warehouse": "WH-A", "material": "RM",
                        "action_seq": 2},
                       {"warehouse": "WH-A", "material": "RM",
                        "action_seq": 3}],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 5}],
        }])[0]
        report = analyze(flow, [order], Ref("WH-A", "RM", 1),
                         normalize([(5, 15)]))
        # J2 已全退，实际投料只有 J1 的 [0,10)：召回 [5,15) 命中 [5,10)
        self.assertEqual(_receipts(report), [("WO-X", "MA", [(5, 10)], 5)])

    def test_ordered_quantity_mismatch(self):
        actions = [_action({
            "seq": 1, "action_id": "R1", "op": "receive", "material": "RM",
            "warehouse": "WH-A", "batch": "RA", "age_start": "2026-01-01",
            "qty": 10, "location": "A"}),
            _action({"seq": 2, "action_id": "J1", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J1", "qty": 5}),
            _action({"seq": 3, "action_id": "P1", "op": "receive",
                     "material": "MA", "warehouse": "WH-A", "batch": "PA",
                     "age_start": "2026-01-02", "qty": 6, "location": "A"})]
        flow = run(actions)
        order = parse_production_obj([{
            "doc_no": "WO-X", "process": "ordered",
            "issues": [{"warehouse": "WH-A", "material": "RM",
                        "action_seq": 2}],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 3}],
        }])[0]
        with self.assertRaisesRegex(RecallError, "数量不符"):
            analyze(flow, [order], Ref("WH-A", "RM", 1), normalize([(0, 5)]))

    def test_mixed_does_not_require_equal_quantities(self):
        # 混料：投入 5、产出 6 合法；只要命中就整单冻结。
        actions = [_action({
            "seq": 1, "action_id": "R1", "op": "receive", "material": "RM",
            "warehouse": "WH-A", "batch": "RA", "age_start": "2026-01-01",
            "qty": 10, "location": "A"}),
            _action({"seq": 2, "action_id": "J1", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J1", "qty": 5}),
            _action({"seq": 3, "action_id": "P1", "op": "receive",
                     "material": "MA", "warehouse": "WH-A", "batch": "PA",
                     "age_start": "2026-01-02", "qty": 6, "location": "A"})]
        flow = run(actions)
        order = parse_production_obj([{
            "doc_no": "WO-X", "process": "mixed",
            "issues": [{"warehouse": "WH-A", "material": "RM",
                        "action_seq": 2}],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 3}],
        }])[0]
        report = analyze(flow, [order], Ref("WH-A", "RM", 1),
                         normalize([(0, 1)]))
        self.assertEqual(_receipts(report), [("WO-X", "MA", [(0, 6)], 6)])

    def test_process_dependency_cycle(self):
        actions = [_action({
            "seq": 1, "action_id": "R1", "op": "receive", "material": "RM",
            "warehouse": "WH-A", "batch": "RA", "age_start": "2026-01-01",
            "qty": 10, "location": "A"}),
            _action({"seq": 2, "action_id": "J1", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J1", "qty": 5}),
            _action({"seq": 3, "action_id": "P1", "op": "receive",
                     "material": "MA", "warehouse": "WH-A", "batch": "PA",
                     "age_start": "2026-01-02", "qty": 5, "location": "A"}),
            _action({"seq": 4, "action_id": "J2", "op": "issue",
                     "material": "MA", "warehouse": "WH-A",
                     "ref_doc": "J2", "qty": 5}),
            _action({"seq": 5, "action_id": "P2", "op": "receive",
                     "material": "MB", "warehouse": "WH-B", "batch": "PB",
                     "age_start": "2026-01-03", "qty": 5, "location": "B"}),
            _action({"seq": 6, "action_id": "J3", "op": "issue",
                     "material": "MB", "warehouse": "WH-B",
                     "ref_doc": "J3", "qty": 5})]
        flow = run(actions)
        work_orders = parse_production_obj([
            {"doc_no": "WO-X", "process": "ordered",
             "issues": [{"warehouse": "WH-A", "material": "RM",
                         "action_seq": 2},
                        {"warehouse": "WH-A", "material": "MA",
                         "action_seq": 4}],
             "receipts": [{"warehouse": "WH-B", "material": "MB",
                           "action_seq": 5}]},
            {"doc_no": "WO-Y", "process": "ordered",
             "issues": [{"warehouse": "WH-B", "material": "MB",
                         "action_seq": 6}],
             "receipts": [{"warehouse": "WH-A", "material": "MA",
                           "action_seq": 3}]},
        ])
        with self.assertRaisesRegex(RecallError, "成环"):
            analyze(flow, work_orders, Ref("WH-A", "RM", 1),
                    normalize([(0, 5)]))


class TestCLI(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = recall_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_success_emits_json_only(self):
        code, out, err = self._run(["--flow", FLOW, "--production", PRODUCTION,
                                    "--request", REQUEST_AB])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual([a["work_order"] for a in payload["affected_receipts"]],
                         ["WO-A", "WO-B", "WO-F", "WO-D2"])

    def test_failure_has_no_partial_list(self):
        with tempfile.TemporaryDirectory() as td:
            bad = _write(td, "req.json", {
                "receipt": {"warehouse": "WH-A", "material": "RM",
                            "action_seq": 1},
                "segments": [[0, 999]],
            })
            code, out, err = self._run(["--flow", FLOW, "--production",
                                        PRODUCTION, "--request", bad])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("越界", err)


class TestScale(unittest.TestCase):
    """区段处理：工作量与对象数不随件数线性增长。"""

    def test_huge_segment_stays_a_few_objects(self):
        n = 3_000_000
        actions = [_action({
            "seq": 1, "action_id": "R", "op": "receive", "material": "RM",
            "warehouse": "WH-A", "batch": "RA", "age_start": "2026-01-01",
            "qty": n, "location": "A"}),
            _action({"seq": 2, "action_id": "J", "op": "issue",
                     "material": "RM", "warehouse": "WH-A",
                     "ref_doc": "J", "qty": n}),
            _action({"seq": 3, "action_id": "P", "op": "receive",
                     "material": "MA", "warehouse": "WH-A", "batch": "PA",
                     "age_start": "2026-01-02", "qty": n, "location": "A"})]
        flow = run(actions)
        order = parse_production_obj([{
            "doc_no": "WO-BIG", "process": "ordered",
            "issues": [{"warehouse": "WH-A", "material": "RM",
                        "action_seq": 2}],
            "receipts": [{"warehouse": "WH-A", "material": "MA",
                          "action_seq": 3}],
        }])[0]
        report = analyze(flow, [order], Ref("WH-A", "RM", 1),
                         normalize([(1, n - 1)]))
        self.assertEqual(_receipts(report),
                         [("WO-BIG", "MA", [(1, n - 1)], n - 2)])
        self.assertLessEqual(len(report.affected[0].segments), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
