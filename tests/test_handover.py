"""交接对账测试：版本裁定、两票争用同一收料、退料坐标、错误与规模。

运行（仓库根目录）::

    python3 -I -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from req_slicer.engine.inputs import parse_file
from req_slicer.engine.runner import run
from req_slicer.handover import cli as handover_cli
from req_slicer.handover.engine import analyze, run_handover
from req_slicer.handover.inputs import load_handover, parse_limit, resolve_versions
from req_slicer.handover.model import HandoverError, Ref

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")

FLOW = os.path.join(SAMPLES, "handover_flow.json")
MAIN = os.path.join(SAMPLES, "handover_main.json")
DISPUTE_FLOW = os.path.join(SAMPLES, "handover_dispute_flow.json")
DISPUTE = os.path.join(SAMPLES, "handover_dispute.json")

T1_TRANSFER = {"doc_no": "T1",
               "issue": {"warehouse": "WH-A", "material": "RM",
                         "action_seq": 3},
               "dest_warehouse": "WH-C"}


def _write(dirname, name, payload):
    path = os.path.join(dirname, name)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh, ensure_ascii=False)
    return path


def _flow(path=FLOW):
    return run(parse_file(path))


def _segments(report, doc_no):
    tr = next(t for t in report.transfers if t.doc_no == doc_no)
    return [(s.ship_start, s.ship_end, s.status) for s in tr.segments]


def _segment(report, doc_no, start):
    tr = next(t for t in report.transfers if t.doc_no == doc_no)
    return next(s for s in tr.segments if s.ship_start == start)


def _handover(tmp, vouchers, transfers=None, name="handover.json"):
    return _write(tmp, name,
                  {"transfers": transfers or [T1_TRANSFER],
                   "vouchers": vouchers})


def _signed(voucher_no, publish_no, doc="T1", seg=(0, 2), receipt_seq=10,
            start=0, warehouse="WH-C", material="RM", status="signed"):
    if status == "void":
        return {"voucher_no": voucher_no, "publish_no": publish_no,
                "status": "void"}
    return {"voucher_no": voucher_no, "publish_no": publish_no,
            "status": status, "doc_no": doc,
            "ship_segment": [seg[0], seg[1]],
            "receipt": {"warehouse": warehouse, "material": material,
                        "action_seq": receipt_seq, "start": start}}


class TestVersionResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _records(self, vouchers):
        return load_handover(_handover(self.tmp.name, vouchers))[1]

    def test_highest_publish_within_limit_wins(self):
        recs = self._records([
            _signed("V1", 1, seg=(0, 2)),
            _signed("V1", 5, seg=(0, 4)),
            _signed("V1", 9, seg=(0, 6)),   # 上限外
        ])
        effective = resolve_versions(recs, 5)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["publish_no"], 5)
        self.assertEqual(effective[0]["content"][2:4], (0, 4))

    def test_void_is_not_a_claim(self):
        recs = self._records([_signed("V1", 1), _signed("V1", 2, status="void")])
        effective = resolve_versions(recs, 2)
        self.assertEqual(effective[0]["content"], ("void",))
        report = analyze(_flow(), *_load_docs(self.tmp.name), limit=2)
        self.assertEqual(_segments(report, "T1")[0], (0, 5, "PENDING"))

    def test_identical_duplicate_counts_once(self):
        recs = self._records([_signed("V1", 3), _signed("V1", 3)])
        self.assertEqual(len(resolve_versions(recs, 3)), 1)

    def test_same_version_different_content_fails(self):
        recs = self._records([_signed("V1", 3, seg=(0, 2)),
                              _signed("V1", 3, seg=(0, 3))])
        with self.assertRaises(HandoverError) as ctx:
            resolve_versions(recs, 3)
        self.assertIn("内容矛盾", str(ctx.exception))

    def test_conflict_beyond_limit_is_ignored(self):
        recs = self._records([_signed("V1", 5, seg=(0, 2)),
                              _signed("V1", 5, seg=(0, 3))])
        self.assertEqual(resolve_versions(recs, 4), [])
        with self.assertRaises(HandoverError):
            resolve_versions(recs, 5)

    def test_line_order_does_not_matter(self):
        base = [_signed("V1", 1, seg=(0, 5)),
                _signed("V1", 2, seg=(0, 6)),
                _signed("V2", 1, seg=(6, 8), start=6)]
        reports = []
        for shuffle in (False, True):
            vouchers = list(base)
            if shuffle:
                random.Random(7).shuffle(vouchers)
            path = _handover(self.tmp.name, vouchers, name=f"h{shuffle}.json")
            reports.append(run_handover(FLOW, path, 2).as_json())
        self.assertEqual(reports[0], reports[1])


def _load_docs(dirname):
    return load_handover(os.path.join(dirname, "handover.json"))


class TestReturnCoordinates(unittest.TestCase):
    """12 件发货 = 交付行 RA1[0,5) + RA2[0,7)；退 3 件在发货坐标 [9,12)。"""

    def test_returned_positions_are_listed_but_not_claimable(self):
        report = run_handover(FLOW, MAIN, 4)
        self.assertEqual(_segments(report, "T1"), [
            (0, 2, "RECEIVED"), (2, 5, "DISPUTED"),
            (5, 6, "RECEIVED"), (6, 9, "PENDING"), (9, 12, "RETURNED")])
        ret = _segment(report, "T1", 9)
        self.assertEqual(ret.claims, [])
        self.assertIsNone(ret.source_receipt)

    def test_claim_on_returned_tail_does_not_occupy_receipt(self):
        # V1 覆盖发货 [7, 11)：[9, 11) 已退料，只剩 [7, 9) 参与对账。
        # 映射不因退料压缩，故 [7,9) 仍对应收料 [0,2)。
        with tempfile.TemporaryDirectory() as tmp:
            path = _handover(tmp, [_signed("V1", 1, seg=(7, 11), start=0)])
            report = run_handover(FLOW, path, 1)
        self.assertEqual(_segments(report, "T1"), [
            (0, 5, "PENDING"), (5, 7, "PENDING"), (7, 9, "RECEIVED"),
            (9, 12, "RETURNED")])
        got = _segment(report, "T1", 7)
        self.assertEqual((got.source_start, got.source_end), (0, 2))

    def test_source_coordinates_across_delivery_lines(self):
        # V1 [3, 10) 跨交付行边界 5：发货 [3,5)->收料 [0,2)，[5,9)->[2,6)
        with tempfile.TemporaryDirectory() as tmp:
            path = _handover(tmp, [_signed("V1", 1, seg=(3, 10), start=0)])
            report = run_handover(FLOW, path, 1)
        self.assertEqual(_segments(report, "T1"), [
            (0, 3, "PENDING"), (3, 5, "RECEIVED"), (5, 9, "RECEIVED"),
            (9, 12, "RETURNED")])
        first = _segment(report, "T1", 3)
        second = _segment(report, "T1", 5)
        self.assertEqual((first.source_start, first.source_end), (0, 2))
        self.assertEqual((second.source_start, second.source_end), (2, 6))
        self.assertEqual(first.source_receipt, Ref("WH-C", "RM", 10))


class TestTwoTransfersSameReceipt(unittest.TestCase):
    """两票争用同一笔收料的局部区段；改签一份凭证即同时解除两边争议。"""

    @classmethod
    def setUpClass(cls):
        cls.old = run_handover(DISPUTE_FLOW, DISPUTE, 1)
        cls.new = run_handover(DISPUTE_FLOW, DISPUTE, 2)

    def test_both_sides_show_dispute_at_same_receipt_coordinates(self):
        t1 = _segment(self.old, "T1", 6)
        t2 = _segment(self.old, "T2", 0)
        self.assertEqual(t1.status, "DISPUTED")
        self.assertEqual(t2.status, "DISPUTED")
        # 收料侧重叠坐标相同：RC1[6,10)，两边都列对方凭证号
        for seg in (t1, t2):
            detail = seg.conflict["receipts"][0]
            self.assertEqual((detail["start"], detail["end"]), (6, 10))
            self.assertEqual(detail["receipt"],
                             Ref("WH-C", "RM", 10).as_json())
            self.assertEqual(detail["vouchers"], ["V1", "V2"])
        # 发货侧重叠坐标：T1 是 [6,10)，T2 是 [0,4)
        self.assertEqual((t1.conflict["ship"]["start"],
                          t1.conflict["ship"]["end"]), (6, 10))
        self.assertEqual((t2.conflict["ship"]["start"],
                          t2.conflict["ship"]["end"]), (0, 4))
        self.assertEqual(t1.others, ["V2"])
        self.assertEqual(t2.others, ["V1"])

    def test_only_v2_reissued_clears_both_transfers(self):
        # 上限 2 只让 V2 有了更高发布号（改签到 RC2）；V1 没有改。
        self.assertEqual(_segments(self.new, "T1"), [
            (0, 5, "RECEIVED"), (5, 10, "RECEIVED"), (10, 12, "PENDING")])
        self.assertEqual(_segments(self.new, "T2"), [
            (0, 4, "RECEIVED"), (4, 6, "RETURNED")])
        t1 = _segment(self.new, "T1", 5)
        self.assertEqual((t1.source_receipt, t1.source_start, t1.source_end),
                         (Ref("WH-C", "RM", 10), 5, 10))
        self.assertEqual(t1.others, [])
        t2 = _segment(self.new, "T2", 0)
        self.assertEqual(t2.source_receipt, Ref("WH-C", "RM", 11))

    def test_old_limit_reproduces_before_reissue(self):
        again = run_handover(DISPUTE_FLOW, DISPUTE, 1)
        self.assertEqual(again.as_json(), self.old.as_json())


class TestHandoverErrors(unittest.TestCase):
    def _expect(self, vouchers, flow=FLOW, limit=1, transfers=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = _handover(tmp, vouchers, transfers=transfers)
            with self.assertRaises(HandoverError) as ctx:
                run_handover(flow, path, limit)
        return str(ctx.exception)

    def test_ship_segment_out_of_bounds(self):
        self.assertIn("发货区段 [3, 13) 越界",
                      self._expect([_signed("V1", 1, seg=(3, 13))]))

    def test_receipt_segment_out_of_bounds_not_clipped(self):
        self.assertIn("收料区段 [14, 21) 越界",
                      self._expect([_signed("V1", 1, seg=(3, 10), start=14)]))

    def test_receipt_must_be_at_dest_warehouse(self):
        self.assertIn("不是调拨 'T1' 的指定收仓",
                      self._expect([_signed("V1", 1, warehouse="WH-B")]))

    def test_receipt_must_be_same_material(self):
        self.assertIn("物料", self._expect([_signed("V1", 1, material="OTHER")]))

    def test_unknown_doc_no(self):
        self.assertIn("不存在的调拨号 'T9'",
                      self._expect([_signed("V1", 1, doc="T9")]))

    def test_invalid_issue_ref(self):
        transfers = [{"doc_no": "T1",
                      "issue": {"warehouse": "WH-A", "material": "RM",
                                "action_seq": 99},
                      "dest_warehouse": "WH-C"}]
        msg = self._expect([], transfers=transfers)
        self.assertIn("领料引用", msg)
        self.assertIn("不存在", msg)

    def test_same_issue_in_two_transfers(self):
        transfers = [T1_TRANSFER, {**T1_TRANSFER, "doc_no": "T2"}]
        self.assertIn("同一领料不得列在两票",
                      self._expect([], transfers=transfers))

    def test_dest_equals_origin(self):
        transfers = [{**T1_TRANSFER, "dest_warehouse": "WH-A"}]
        self.assertIn("不能是发仓", self._expect([], transfers=transfers))

    def test_flow_breakpoint_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = _write(tmp, "broken.json", [
                {"seq": 1, "action_id": "r", "op": "receive",
                 "material": "RM", "warehouse": "WH-A", "batch": "B",
                 "age_start": "2026-01-01", "qty": 5, "location": "A"},
                {"seq": 2, "action_id": "i", "op": "issue",
                 "material": "RM", "warehouse": "WH-A", "ref_doc": "D",
                 "qty": 9},
            ])
            path = _handover(tmp, [])
            with self.assertRaises(HandoverError) as ctx:
                run_handover(flow, path, 1)
        self.assertIn("断点", str(ctx.exception))
        self.assertIn("seq=2", str(ctx.exception))

    def test_bool_publish_no_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _handover(tmp, [{"voucher_no": "V1", "publish_no": True,
                                    "status": "void"}])
            with self.assertRaises(HandoverError):
                load_handover(path)

    def test_negative_limit_rejected(self):
        for bad in (-1, "abc", 1.5, True):
            with self.assertRaises(HandoverError):
                parse_limit(bad)


class TestCli(unittest.TestCase):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = handover_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_success_emits_json_and_zero(self):
        code, out, err = self._run("--flow", FLOW, "--handover", MAIN,
                                   "--publish-limit", "4")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["publish_limit"], 4)
        self.assertEqual(payload["transfers"][0]["doc_no"], "T1")

    def test_failure_emits_no_list_and_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _handover(tmp, [_signed("V1", 1, seg=(0, 99))])
            code, out, err = self._run("--flow", FLOW, "--handover", path,
                                       "--publish-limit", "1")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("[错误]", err)

    def test_out_file_written_only_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "report.json")
            code, out, _ = self._run("--flow", FLOW, "--handover", MAIN,
                                     "--publish-limit", "1", "--out", target)
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), json.loads(out))


class TestLargeQuantitiesAreNotExpanded(unittest.TestCase):
    def test_million_scale_segments_stay_few(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = _write(tmp, "big.json", [
                {"seq": 1, "action_id": "r1", "op": "receive",
                 "material": "RM", "warehouse": "WH-A", "batch": "B1",
                 "age_start": "2026-01-01", "qty": 2_000_000, "location": "A"},
                {"seq": 2, "action_id": "r2", "op": "receive",
                 "material": "RM", "warehouse": "WH-A", "batch": "B2",
                 "age_start": "2026-01-02", "qty": 2_000_000, "location": "A"},
                {"seq": 3, "action_id": "i1", "op": "issue",
                 "material": "RM", "warehouse": "WH-A", "ref_doc": "D1",
                 "qty": 3_000_000},
                {"seq": 4, "action_id": "rt", "op": "return",
                 "material": "RM", "warehouse": "WH-A", "ref_doc": "D1",
                 "qty": 1, "location": "A"},
                {"seq": 10, "action_id": "rc", "op": "receive",
                 "material": "RM", "warehouse": "WH-C", "batch": "C",
                 "age_start": "2026-01-03", "qty": 3_000_000, "location": "C"},
            ])
            path = _handover(tmp, [
                _signed("V1", 1, seg=(0, 1_500_000), start=0),
                _signed("V2", 1, seg=(1_000_000, 2_000_000), start=0),
            ])
            started = time.time()
            report = run_handover(flow, path, 1)
            elapsed = time.time() - started
        tr = report.transfers[0]
        self.assertEqual(_segments(report, "T1"), [
            (0, 1_000_000, "DISPUTED"),
            (1_000_000, 1_500_000, "DISPUTED"),
            (1_500_000, 2_000_000, "DISPUTED"),
            (2_000_000, 2_999_999, "PENDING"),
            (2_999_999, 3_000_000, "RETURNED"),
        ])
        self.assertEqual(tr.ship_qty, 3_000_000)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
