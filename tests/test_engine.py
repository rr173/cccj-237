"""引擎测试：手算核对、断点语义、守恒与百万级切片。

直接在仓库根目录运行（离线、无第三方依赖）::

    python3 -I -m unittest discover -s tests -t . -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from req_slicer.engine.inputs import parse_file
from req_slicer.engine.model import (Action, MaterialLedger, FlowError,
                                     RECEIVE, TRANSFER, ISOLATE, RELEASE,
                                     ISSUE, RETURN, NORMAL, ISOLATED, ISSUED)
from req_slicer.engine.runner import run
from req_slicer.engine.outputs import write_outputs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def act(seq, action_id, op, material="M", warehouse="WH", **kw):
    return Action(seq=seq, action_id=action_id, op=op,
                  material=material, warehouse=warehouse, **kw)


def run_ledger(actions, material="M", warehouse="WH"):
    lg = MaterialLedger(material, warehouse)
    for a in actions:
        lg.apply(a)
    return lg


def slice_keys(ledger, state=None):
    out = [(s.batch, s.receipt_seq, s.seg_start, s.seg_end, s.qty,
            s.location, s.holder)
           for s in ledger.live_slices()
           if state is None or s.state == state]
    return sorted(out, key=lambda r: (r[1], r[2]))


class TestMainWorkedExample(unittest.TestCase):
    """samples/flow_main.json 的完整手算核对（见 docs/hand_calc.md）。"""

    @classmethod
    def setUpClass(cls):
        cls.actions = parse_file(os.path.join(SAMPLES, "flow_main.json"))
        cls.result = run(cls.actions)
        cls.gr = next(g for g in cls.result.groups
                      if g.material == "M1" and g.warehouse == "WH1")
        cls.lg = cls.gr.ledger

    def test_no_breakpoint_and_all_actions_applied(self):
        self.assertIsNone(self.gr.error)
        self.assertEqual(self.gr.processed, 12)

    def test_delivery_lines_exact_batch_slices(self):
        got = [(l.doc_no, l.batch, l.receipt_seq, l.seg_start, l.seg_end,
                l.qty, l.returned_qty, l.outstanding_qty)
               for l in self.lg.lines]
        expected = [
            ("D1", "B-OLD", 1, 60, 100, 40, 20, 20),
            ("D1", "B-NEW", 2, 0, 10, 10, 10, 0),
            ("D2", "B-OLD", 1, 0, 40, 40, 13, 27),
            ("D2", "B-NEW", 2, 10, 40, 30, 30, 0),
            ("D3", "B-OLD", 1, 40, 60, 20, 0, 20),
            ("D3", "B-NEW", 2, 40, 50, 10, 0, 10),
        ]
        self.assertEqual(got, expected)

    def test_period_end_issued_grouped_by_doc(self):
        got = [(s.holder, s.receipt_seq, s.seg_start, s.seg_end, s.qty)
               for s in self.lg.live_slices() if s.state == ISSUED]
        self.assertEqual(sorted(got), [
            ("D1", 1, 60, 80, 20),
            ("D2", 1, 0, 27, 27),
            ("D3", 1, 40, 60, 20),
            ("D3", 2, 40, 50, 10),
        ])

    def test_period_end_on_hand_with_locations(self):
        got = sorted((s.location, s.receipt_seq, s.seg_start, s.seg_end, s.qty)
                     for s in self.lg.live_slices() if s.state == NORMAL)
        self.assertEqual(got, [
            ("A", 2, 60, 80, 20),
            ("L1", 1, 27, 40, 13),
            ("L1", 2, 10, 15, 5),
            ("L1", 2, 15, 40, 25),
            ("L1", 2, 50, 60, 10),
            ("L2", 1, 80, 100, 20),
            ("L2", 2, 0, 10, 10),
        ])

    def test_no_slice_left_in_isolation(self):
        self.assertEqual(
            [s for s in self.lg.live_slices() if s.state == ISOLATED], [])

    def test_conservation_and_segment_coverage(self):
        rec = self.lg.reconciliation()
        self.assertTrue(rec["conserved"])
        self.assertTrue(rec["coverage_ok"])
        self.assertEqual(rec["received_total"], 180)
        self.assertEqual(rec["live_total"], 180)
        self.assertEqual(rec["state_totals"][NORMAL], 103)
        self.assertEqual(rec["state_totals"][ISSUED], 77)
        self.assertEqual(rec["state_totals"][ISOLATED], 0)

    def test_location_change_keeps_age_and_identity(self):
        # D3 领料时，L2 的老批次 B-OLD（货龄 2026-01-10）虽经隔离/放行/换库位，
        # 仍按货龄优先于 L1/A 的 B-NEW 被取走。
        d3 = [l for l in self.lg.lines if l.doc_no == "D3"]
        self.assertEqual([(l.batch, l.age_start, l.qty) for l in d3],
                         [("B-OLD", "2026-01-10", 20),
                          ("B-NEW", "2026-02-15", 10)])


class TestSmallHandCalculation(unittest.TestCase):
    """小数量独立手算：隔离->领用->放行->退料，逐步核对。"""

    def test_step_by_step(self):
        lg = MaterialLedger("HM", "WH")
        lg.apply(act(1, "r1", RECEIVE, "HM", "WH", batch="X",
                     age_start="2026-01-01", qty=5, location="A"))
        lg.apply(act(2, "r2", RECEIVE, "HM", "WH", batch="Y",
                     age_start="2026-02-01", qty=5, location="A"))
        lg.apply(act(3, "i1", ISOLATE, "HM", "WH", qty=3, from_location="A"))
        # 隔离从最老批次起点裁 [0,3)
        self.assertEqual(
            [(s.receipt_seq, s.seg_start, s.seg_end) for s in lg.isolated["i1"]],
            [(1, 0, 3)])

        lg.apply(act(4, "d1", ISSUE, "HM", "WH", ref_doc="D1", qty=4))
        # 正常库存只剩 X[3,5) 与 Y 整段 -> 取 X[3,5) + Y[0,2)
        self.assertEqual(
            [(l.batch, l.seg_start, l.seg_end, l.qty) for l in lg.lines],
            [("X", 3, 5, 2), ("Y", 0, 2, 2)])

        lg.apply(act(5, "rel1", RELEASE, "HM", "WH", qty=2,
                     isolate_id="i1", location="A"))
        # 放行只在 i1 尚持有的 [0,3) 内从原顺序起点取 [0,2)
        self.assertEqual(
            [(s.seg_start, s.seg_end, s.location)
             for s in lg.normal if s.batch == "X"],
            [(0, 2, "A"), ])
        self.assertEqual(
            [(s.seg_start, s.seg_end) for s in lg.isolated["i1"]], [(2, 3)])

        lg.apply(act(6, "rt1", RETURN, "HM", "WH",
                     ref_doc="D1", qty=3, location="B"))
        # 从最后取走的切片向前退：Y[0,2) 全退，再从 X[3,5) 末端退 1
        self.assertEqual(
            sorted((s.location, s.batch, s.seg_start, s.seg_end)
                   for s in lg.normal),
            [("A", "X", 0, 2), ("A", "Y", 2, 5),
             ("B", "X", 4, 5), ("B", "Y", 0, 2)])
        # D1 仍占用 X[3,4)
        self.assertEqual(
            [(s.batch, s.seg_start, s.seg_end) for s in lg.issued["D1"]],
            [("X", 3, 4)])
        rec = lg.reconciliation()
        self.assertTrue(rec["conserved"] and rec["coverage_ok"])
        self.assertEqual(rec["live_total"], 10)


class TestReissueSameSegmentAfterReturn(unittest.TestCase):
    """退料后同单号再次领用同一原料段：两行交付坐标相同，退回累计不得串线。"""

    def test_return_attributed_to_correct_line(self):
        lg = MaterialLedger("R", "WH")
        lg.apply(act(1, "r1", RECEIVE, "R", "WH", batch="B",
                     age_start="2026-01-01", qty=10, location="A"))
        lg.apply(act(2, "d1", ISSUE, "R", "WH", ref_doc="D1", qty=10))
        lg.apply(act(3, "rt1", RETURN, "R", "WH",
                     ref_doc="D1", qty=10, location="A"))
        # 退回的 10 件重新被同一单号领走，产生坐标相同但 line_id 不同的新交付行
        lg.apply(act(4, "d2", ISSUE, "R", "WH", ref_doc="D1", qty=10))
        self.assertEqual([l.line_id for l in lg.lines], [0, 1])
        # 只退第二行 3 件：第一行保持已全退，第二行未退 7
        lg.apply(act(5, "rt2", RETURN, "R", "WH",
                     ref_doc="D1", qty=3, location="A"))
        self.assertEqual(
            [(l.line_id, l.returned_qty, l.outstanding_qty) for l in lg.lines],
            [(0, 10, 0), (1, 3, 7)])
        rec = lg.reconciliation()
        self.assertTrue(rec["conserved"] and rec["coverage_ok"])


class TestSameTotalDifferentBatches(unittest.TestCase):
    """合计数量相同，不代表取自相同批次。"""

    def _issue_ten(self, old_qty, new_qty):
        lg = MaterialLedger("Z", "WH")
        lg.apply(act(1, "r1", RECEIVE, "Z", "WH", batch="OLD",
                     age_start="2026-01-01", qty=old_qty, location="A"))
        lg.apply(act(2, "r2", RECEIVE, "Z", "WH", batch="NEW",
                     age_start="2026-03-01", qty=new_qty, location="A"))
        lg.apply(act(3, "d", ISSUE, "Z", "WH", ref_doc="D", qty=10))
        return [(l.batch, l.qty) for l in lg.lines]

    def test_composition_differs(self):
        p = self._issue_ten(10, 10)   # 老批次足够：10 全老
        q = self._issue_ten(4, 16)    # 老批次只有 4：老 4 + 新 6
        self.assertEqual(p, [("OLD", 10)])
        self.assertEqual(q, [("OLD", 4), ("NEW", 6)])


class TestBreakpoints(unittest.TestCase):
    def test_unknown_return_reference(self):
        lg = MaterialLedger("M", "WH")
        lg.apply(act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                     qty=10, location="A"))
        lg.apply(act(2, "d1", ISSUE, ref_doc="D1", qty=4))
        with self.assertRaisesRegex(FlowError, "NO-SUCH"):
            lg.apply(act(3, "rt", RETURN, ref_doc="NO-SUCH",
                         qty=1, location="A"))

    def test_over_return_rejected_and_state_unchanged(self):
        lg = MaterialLedger("M", "WH")
        lg.apply(act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                     qty=10, location="A"))
        lg.apply(act(2, "d1", ISSUE, ref_doc="D1", qty=4))
        before = slice_keys(lg)
        with self.assertRaisesRegex(FlowError, "超量退料"):
            lg.apply(act(3, "rt", RETURN, ref_doc="D1", qty=5, location="A"))
        self.assertEqual(slice_keys(lg), before)  # 失败动作无副作用

    def test_cross_material_reference_rejected(self):
        result = run([
            act(1, "r1", RECEIVE, "M1", "WH", batch="B",
                age_start="2026-01-01", qty=10, location="A"),
            act(2, "d1", ISSUE, "M1", "WH", ref_doc="D1", qty=4),
            act(3, "r2", RECEIVE, "M2", "WH", batch="C",
                age_start="2026-01-01", qty=10, location="A"),
            act(4, "rt", RETURN, "M2", "WH", ref_doc="D1",
                qty=1, location="A"),
        ])
        g2 = next(g for g in result.groups if g.material == "M2")
        self.assertIn("不存在", g2.error)
        self.assertEqual(g2.processed, 1)  # 断点前收料保留
        g1 = next(g for g in result.groups if g.material == "M1")
        self.assertIsNone(g1.error)        # 不同物料独立

    def test_release_unknown_or_exhausted_isolate(self):
        lg = MaterialLedger("M", "WH")
        lg.apply(act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                     qty=10, location="A"))
        lg.apply(act(2, "i1", ISOLATE, qty=3, from_location="A"))
        with self.assertRaisesRegex(FlowError, "不存在"):
            lg.apply(act(3, "rel", RELEASE, qty=1,
                         isolate_id="GHOST", location="A"))
        lg.apply(act(4, "rel1", RELEASE, qty=3,
                     isolate_id="i1", location="A"))
        with self.assertRaisesRegex(FlowError, "已无持有"):
            lg.apply(act(5, "rel2", RELEASE, qty=1,
                         isolate_id="i1", location="A"))

    def test_insufficient_issue_leaves_no_trace(self):
        lg = MaterialLedger("M", "WH")
        lg.apply(act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                     qty=5, location="A"))
        before = slice_keys(lg)
        with self.assertRaisesRegex(FlowError, "数量不足"):
            lg.apply(act(2, "d", ISSUE, ref_doc="D", qty=6))
        self.assertEqual(slice_keys(lg), before)
        self.assertEqual(lg.lines, [])

    def test_duplicate_identical_action_runs_once(self):
        result = run([
            act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                qty=10, location="A"),
            act(2, "d1", ISSUE, ref_doc="D1", qty=4),
            act(3, "d1", ISSUE, ref_doc="D1", qty=4),  # 同身份同内容
        ])
        g = result.groups[0]
        self.assertIsNone(g.error)
        self.assertEqual(g.skipped_duplicates, 1)
        self.assertEqual(sum(l.qty for l in g.ledger.lines), 4)

    def test_duplicate_id_different_content_is_breakpoint(self):
        result = run([
            act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                qty=10, location="A"),
            act(2, "d1", ISSUE, ref_doc="D1", qty=4),
            act(3, "d1", ISSUE, ref_doc="D1", qty=5),  # 同身份不同内容
        ])
        g = result.groups[0]
        self.assertIn("内容不同", g.error)
        self.assertEqual(g.error_seq, 3)

    def test_seq_conflict_is_first_breakpoint(self):
        result = run([
            act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                qty=10, location="A"),
            act(1, "r2", RECEIVE, batch="C", age_start="2026-01-02",
                qty=10, location="A"),
        ])
        g = result.groups[0]
        self.assertIn("顺序号冲突", g.error)
        self.assertEqual(g.processed, 0)

    def test_transfer_with_insufficient_location_stock(self):
        lg = MaterialLedger("M", "WH")
        lg.apply(act(1, "r1", RECEIVE, batch="B", age_start="2026-01-01",
                     qty=5, location="A"))
        with self.assertRaisesRegex(FlowError, "数量不足"):
            lg.apply(act(2, "t", TRANSFER, qty=6,
                         from_location="A", location="L1"))

    def test_sample_breakpoint_file_retains_prior_results(self):
        result = run(parse_file(os.path.join(SAMPLES, "flow_breakpoints.json")))
        g = result.groups[0]
        self.assertIn("NO-SUCH-DOC", g.error)
        self.assertEqual(g.processed, 3)       # 前 3 条动作有效
        self.assertEqual(g.skipped_duplicates, 1)
        # 断点前：两次各领 40，在库剩 20
        rec = g.ledger.reconciliation()
        self.assertEqual(rec["state_totals"][NORMAL], 20)
        self.assertEqual(rec["state_totals"][ISSUED], 80)

    def test_sample_seq_conflict_file(self):
        result = run(parse_file(os.path.join(SAMPLES, "flow_seq_conflict.json")))
        self.assertIn("顺序号冲突", result.groups[0].error)


class TestMillionQuantitySlices(unittest.TestCase):
    """数百万数量的切片不得展开成百万行。"""

    def test_bounded_slice_count(self):
        result = run(parse_file(os.path.join(SAMPLES, "flow_million.json")))
        g = result.groups[0]
        self.assertIsNone(g.error)
        live = g.ledger.live_slices()
        self.assertLess(len(live), 10)  # 始终是少量区段对象
        rec = g.ledger.reconciliation()
        self.assertTrue(rec["conserved"] and rec["coverage_ok"])
        self.assertEqual(rec["received_total"], 3_000_000)
        self.assertEqual(rec["state_totals"][ISSUED], 1)
        self.assertEqual(rec["state_totals"][NORMAL], 2_999_999)
        # 原料段位置仍可逐段核验
        issued = [s for s in live if s.state == ISSUED][0]
        self.assertEqual((issued.receipt_seq, issued.seg_start, issued.seg_end),
                         (1, 0, 1))


class TestCSVAndEndToEnd(unittest.TestCase):
    def test_chinese_headered_csv_and_outputs(self):
        csv_text = (
            "序号,动作编号,动作,物料,仓库,批次,收料日期,数量,库位,来源库位,隔离动作,领料单号\n"
            "1,R1,收料,T,W,BT,2026-01-01,10,A,,,\n"
            "2,I1,隔离,T,W,,,5,,A,,\n"
            "3,REL1,放行,T,W,,,5,A,,I1,\n"
            "4,D1,领料,T,W,,,6,,,,D1\n"
            "5,RT1,退料,T,W,,,2,B,,,D1\n"
        )
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "flow.csv")
            out = os.path.join(td, "out")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(csv_text)
            actions = parse_file(src)
            self.assertEqual([a.op for a in actions],
                             [RECEIVE, ISOLATE, RELEASE, ISSUE, RETURN])
            result = run(actions)
            paths = write_outputs(result, out)
            for path in paths.values():
                self.assertTrue(os.path.exists(path))
            with open(paths["errors"], encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["errors"], [])
            g = result.groups[0]
            rec = g.ledger.reconciliation()
            self.assertTrue(rec["conserved"] and rec["coverage_ok"])
            # 领 6（BT[0,6)）退 2（末端 [4,6) 回 B），仍占用 [0,4)
            self.assertEqual(
                [(s.seg_start, s.seg_end) for s in g.ledger.issued["D1"]],
                [(0, 4)])

    def test_rerun_is_deterministic(self):
        actions = parse_file(os.path.join(SAMPLES, "flow_main.json"))
        with tempfile.TemporaryDirectory() as td:
            d1, d2 = os.path.join(td, "a"), os.path.join(td, "b")
            write_outputs(run(actions), d1)
            write_outputs(run(actions), d2)
            for name in os.listdir(d1):
                with open(os.path.join(d1, name), "rb") as f1, \
                     open(os.path.join(d2, name), "rb") as f2:
                    self.assertEqual(f1.read(), f2.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
