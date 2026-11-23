"""离线仓储计费测试：时间语义、跨档/改价、同分钟、缺价、断点与规模。

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
from req_slicer.billing import cli as billing_cli
from req_slicer.billing.engine import BillingLedger, run_billing
from req_slicer.billing.inputs import parse_period, parse_price_book
from req_slicer.billing.model import BillingError, TIER1, TIER2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
FLOW = os.path.join(SAMPLES, "billing_flow.json")
PRICES = os.path.join(SAMPLES, "billing_prices.json")


def _write(td, name, payload):
    path = os.path.join(td, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


def _receive(seq, minute, qty=10, loc="A", wh="WH1", mat="M1"):
    return {"seq": seq, "action_id": f"R{seq}", "op": "receive",
            "material": mat, "warehouse": wh, "batch": f"B{seq}",
            "age_start": "2026-01-01", "qty": qty, "location": loc,
            "minute": minute}


class TestWorkedExample(unittest.TestCase):
    """samples/billing_flow.json 的手算样例（见 docs/billing.md）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = run_billing(FLOW, PRICES, 3, 12)

    def test_total_and_two_locations(self):
        self.assertEqual(self.report.total_fee_micro, 1156)
        locs = sorted(d.location for d in self.report.details)
        self.assertEqual(locs, ["A", "B"])

    def test_piece_minutes_and_fees(self):
        by_loc = {d.location: d for d in self.report.details}
        self.assertEqual(by_loc["A"].piece_minutes,
                         {TIER1: 19, TIER2: 27})
        self.assertEqual(by_loc["A"].fee, {TIER1: 194, TIER2: 522})
        self.assertEqual(by_loc["B"].piece_minutes,
                         {TIER1: 5, TIER2: 27})
        self.assertEqual(by_loc["B"].fee, {TIER1: 35, TIER2: 405})

    def test_sorting_by_warehouse_material_receipt_location(self):
        keys = [(d.warehouse, d.material, d.receipt_seq, d.location)
                for d in self.report.details]
        self.assertEqual(keys, sorted(keys))

    def test_json_shape(self):
        payload = self.report.as_json()
        self.assertEqual(payload["period"], {"start": 3, "end": 12})
        self.assertEqual(payload["threshold_minutes"], 5)
        self.assertEqual(payload["total_fee_micro"], 1156)
        line = payload["details"][0]
        self.assertEqual(set(line),
                         {"warehouse", "material", "receipt_seq", "location",
                          "piece_minutes", "fee_micro", "fee_total_micro"})


class TestSpellTimingSemantics(unittest.TestCase):
    def _spells(self, flow):
        actions = parse_file(_write(self.td, "f.json", flow))
        from req_slicer.engine.runner import plan_groups
        from req_slicer.engine.model import MaterialLedger
        ordered = list(plan_groups(actions).values())[0]
        lg = MaterialLedger("M1", "WH1")
        return BillingLedger(lg).replay(ordered, horizon=12)

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def test_receive_spell_starts_at_receive_minute(self):
        spells = self._spells([_receive(1, 0)])
        self.assertEqual(len(spells), 1)
        sp = spells[0]
        self.assertEqual((sp.start_minute, sp.end_minute, sp.base_accrued),
                         (0, 12, 0))

    def test_isolate_pauses_and_release_resumes_with_same_accrued(self):
        flow = [_receive(1, 0),
                {"seq": 2, "action_id": "I1", "op": "isolate",
                 "material": "M1", "warehouse": "WH1", "qty": 10,
                 "from_location": "A", "minute": 2},
                {"seq": 3, "action_id": "REL1", "op": "release",
                 "material": "M1", "warehouse": "WH1", "qty": 10,
                 "isolate_id": "I1", "location": "A", "minute": 6}]
        spells = self._spells(flow)
        # 两段正常在库 spell：[0,2) base0 与 [6,12) base2；隔离的 [2,6) 不计时长
        self.assertEqual([(s.start_minute, s.end_minute, s.base_accrued)
                          for s in spells], [(0, 2, 0), (6, 12, 2)])

    def test_transfer_keeps_accrued_but_moves_to_new_location(self):
        flow = [_receive(1, 0),
                {"seq": 2, "action_id": "T1", "op": "transfer",
                 "material": "M1", "warehouse": "WH1", "qty": 10,
                 "from_location": "A", "location": "B", "minute": 4}]
        spells = self._spells(flow)
        by_loc = {s.location: s for s in spells}
        self.assertEqual((by_loc["A"].start_minute, by_loc["A"].end_minute),
                         (0, 4))
        # 移位分钟 [4,5) 已归新库位，新 spell 从 4 起、base 接续不清零
        self.assertEqual((by_loc["B"].start_minute, by_loc["B"].base_accrued),
                         (4, 4))

    def test_same_minute_issue_return_has_zero_gap(self):
        flow = [_receive(1, 0, qty=3),
                {"seq": 2, "action_id": "D1", "op": "issue",
                 "material": "M1", "warehouse": "WH1", "ref_doc": "D1",
                 "qty": 3, "minute": 5},
                {"seq": 3, "action_id": "RT1", "op": "return",
                 "material": "M1", "warehouse": "WH1", "ref_doc": "D1",
                 "qty": 3, "location": "A", "minute": 5}]
        spells = self._spells(flow)
        # 同分钟进出：前 spell [0,5)，回库 spell [5,12) base5，中间零时长
        self.assertEqual([(s.start_minute, s.end_minute, s.base_accrued)
                          for s in spells], [(0, 5, 0), (5, 12, 5)])


class TestTierAndPriceBoundaries(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()

    def _report(self, flow, prices, start, end):
        return run_billing(_write(self.td, "f.json", flow),
                           _write(self.td, "p.json", prices), start, end)

    def test_cross_tier_without_any_action(self):
        flow = [_receive(1, 0, qty=2)]
        prices = {"threshold_minutes": 3, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 10, "price_tier2": 20}]}
        r = self._report(flow, prices, 0, 8)
        d = r.details[0]
        # t0..2 一档（2件×3分=6 件分钟），t3..7 二档（2×5=10）
        self.assertEqual(d.piece_minutes, {TIER1: 6, TIER2: 10})
        self.assertEqual(d.fee, {TIER1: 60, TIER2: 200})

    def test_price_change_without_any_action(self):
        flow = [_receive(1, 0, qty=1)]
        prices = {"threshold_minutes": 100, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 10, "price_tier2": 99},
            {"warehouse": "WH1", "location": "A", "effective_minute": 4,
             "price_tier1": 12, "price_tier2": 99}]}
        r = self._report(flow, prices, 0, 8)
        d = r.details[0]
        # 全部一档：t0..3 单价10（40），t4..7 单价12（48）
        self.assertEqual(d.fee[TIER1], 88)
        self.assertEqual(d.piece_minutes[TIER1], 8)

    def test_zero_price_is_valid(self):
        flow = [_receive(1, 0, qty=5)]
        prices = {"threshold_minutes": 2, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 0, "price_tier2": 0}]}
        r = self._report(flow, prices, 0, 6)
        self.assertEqual(r.total_fee_micro, 0)
        self.assertTrue(r.details[0].piece_minutes[TIER1] > 0)


class TestPeriodBoundaries(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()

    def test_period_before_flow_gives_empty_details_zero_fee(self):
        # 账期 [2,4) 内全部处于隔离，没有正常在库
        flow = [_receive(1, 0, qty=5),
                {"seq": 2, "action_id": "I1", "op": "isolate",
                 "material": "M1", "warehouse": "WH1", "qty": 5,
                 "from_location": "A", "minute": 2},
                {"seq": 3, "action_id": "REL1", "op": "release",
                 "material": "M1", "warehouse": "WH1", "qty": 5,
                 "isolate_id": "I1", "location": "A", "minute": 4}]
        prices = {"threshold_minutes": 3, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 1, "price_tier2": 2}]}
        r = run_billing(_write(self.td, "f.json", flow),
                        _write(self.td, "p.json", prices), 2, 4)
        self.assertEqual(r.details, [])
        self.assertEqual(r.total_fee_micro, 0)

    def test_action_at_end_is_validated_but_not_charged(self):
        flow = [_receive(1, 0, qty=3),
                {"seq": 2, "action_id": "D1", "op": "issue",
                 "material": "M1", "warehouse": "WH1", "ref_doc": "D1",
                 "qty": 3, "minute": 5}]
        prices = {"threshold_minutes": 10, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 7, "price_tier2": 9}]}
        # end=5：issue 恰在 end，可验证不收费；正常在库只到 [0,5) 共 15 件分钟
        r = run_billing(_write(self.td, "f.json", flow),
                        _write(self.td, "p.json", prices), 0, 5)
        self.assertEqual(r.details[0].piece_minutes[TIER1], 15)
        # 同流水把 end 放到 6：第 5 分钟已被领料占用，仍不增加正常在库费用
        r2 = run_billing(_write(self.td, "f.json", flow),
                         _write(self.td, "p.json", prices), 0, 6)
        self.assertEqual(r2.total_fee_micro, r.total_fee_micro)

    def test_pre_period_actions_set_initial_accrual_but_not_charged(self):
        # 期前 t0..4 正常在库累计 5 分钟；账期从 5 起，期初时长决定选档
        flow = [_receive(1, 0, qty=1)]
        prices = {"threshold_minutes": 5, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 10, "price_tier2": 20}]}
        r = run_billing(_write(self.td, "f.json", flow),
                        _write(self.td, "p.json", prices), 5, 7)
        # t5 开始累计已达门槛 5 -> 两档分钟全部第二档
        self.assertEqual(r.details[0].piece_minutes, {TIER1: 0, TIER2: 2})
        self.assertEqual(r.details[0].fee[TIER2], 40)


class TestInputValidation(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.good_flow = _write(self.td, "f.json", [_receive(1, 0)])
        self.good_prices = _write(self.td, "p.json", {
            "threshold_minutes": 2, "rates": [
                {"warehouse": "WH1", "location": "A",
                 "effective_minute": 0, "price_tier1": 1,
                 "price_tier2": 2}]})

    def test_period_must_be_non_negative_and_ordered(self):
        for s, e in ((-1, 2), (2, 2), (3, 2), (True, 2), (1.0, 2)):
            with self.assertRaises(BillingError):
                parse_period(s, e)
        self.assertEqual(parse_period(0, 1), (0, 1))

    def test_threshold_must_be_positive_integer(self):
        for bad in (0, -1, True, 1.5, "3"):
            p = _write(self.td, f"p{bad}.json",
                       {"threshold_minutes": bad, "rates": []})
            with self.assertRaises(BillingError):
                parse_price_book(p)

    def test_duplicate_effective_minute_rejected(self):
        p = _write(self.td, "dup.json", {"threshold_minutes": 2, "rates": [
            {"warehouse": "W", "location": "A", "effective_minute": 0,
             "price_tier1": 1, "price_tier2": 2},
            {"warehouse": "W", "location": "A", "effective_minute": 0,
             "price_tier1": 3, "price_tier2": 4}]})
        with self.assertRaisesRegex(BillingError, "生效分钟 0 重复"):
            parse_price_book(p)

    def test_negative_price_rejected_but_zero_allowed(self):
        with self.assertRaises(BillingError):
            parse_price_book(_write(self.td, "neg.json", {
                "threshold_minutes": 1, "rates": [
                    {"warehouse": "W", "location": "A",
                     "effective_minute": 0, "price_tier1": -1,
                     "price_tier2": 2}]}))
        pb = parse_price_book(_write(self.td, "zero.json", {
            "threshold_minutes": 1, "rates": [
                {"warehouse": "W", "location": "A",
                 "effective_minute": 0, "price_tier1": 0,
                 "price_tier2": 0}]}))
        self.assertEqual(pb.threshold_minutes, 1)

    def test_unknown_and_missing_fields_rejected(self):
        with self.assertRaises(BillingError):
            parse_price_book(_write(self.td, "extra.json", {
                "threshold_minutes": 1, "rates": [], "bogus": 1}))
        with self.assertRaises(BillingError):
            parse_price_book(_write(self.td, "miss.json", {"rates": []}))

    def test_missing_minute_on_action_rejected(self):
        flow = [_receive(1, 0)]
        flow.append({"seq": 2, "action_id": "T1", "op": "transfer",
                     "material": "M1", "warehouse": "WH1", "qty": 1,
                     "from_location": "A", "location": "B"})
        with self.assertRaisesRegex(BillingError, "缺少 minute"):
            run_billing(_write(self.td, "nomin.json", flow),
                        self.good_prices, 0, 5)

    def test_minute_going_backwards_rejected(self):
        flow = [_receive(1, 5),
                {"seq": 2, "action_id": "T1", "op": "transfer",
                 "material": "M1", "warehouse": "WH1", "qty": 1,
                 "from_location": "A", "location": "B", "minute": 4}]
        with self.assertRaisesRegex(BillingError, "时间不得倒退"):
            run_billing(_write(self.td, "back.json", flow),
                        self.good_prices, 0, 6)

    def test_minute_later_than_period_end_rejected(self):
        flow = [_receive(1, 0),
                {"seq": 2, "action_id": "T1", "op": "transfer",
                 "material": "M1", "warehouse": "WH1", "qty": 1,
                 "from_location": "A", "location": "B", "minute": 9}]
        with self.assertRaisesRegex(BillingError, "晚于账期末端"):
            run_billing(_write(self.td, "late.json", flow),
                        self.good_prices, 0, 8)

    def test_charged_time_without_price_fails_locating_location_and_minute(self):
        flow = [_receive(1, 0, qty=2),
                {"seq": 2, "action_id": "T1", "op": "transfer",
                 "material": "M1", "warehouse": "WH1", "qty": 2,
                 "from_location": "A", "location": "B", "minute": 4}]
        prices = {"threshold_minutes": 3, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 1, "price_tier2": 2}]}  # 没有 B 的价
        with self.assertRaisesRegex(BillingError, "库位 'B' 在分钟 4"):
            run_billing(_write(self.td, "nob.json", flow),
                        _write(self.td, "nobp.json", prices), 0, 8)

    def test_flow_breakpoint_fails_with_no_partial_bill(self):
        flow = [_receive(1, 0, qty=5),
                {"seq": 2, "action_id": "D1", "op": "issue",
                 "material": "M1", "warehouse": "WH1", "ref_doc": "D1",
                 "qty": 6, "minute": 2}]  # 数量不足 -> 断点
        with self.assertRaisesRegex(BillingError, "断点"):
            run_billing(_write(self.td, "bp.json", flow),
                        self.good_prices, 0, 4)


class TestDuplicateAndReplayParity(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()

    def test_identical_resend_including_minute_runs_once(self):
        # 同 action_id、不同 seq、内容（含 minute 与批次）完全相同 -> 幂等一次
        second = dict(_receive(2, 0))
        second["action_id"] = "R1"
        second["batch"] = "B1"
        flow = [_receive(1, 0), second]
        r = run_billing(_write(self.td, "dup.json", flow),
                        _write(self.td, "p.json", {
                            "threshold_minutes": 5, "rates": [
                                {"warehouse": "WH1", "location": "A",
                                 "effective_minute": 0,
                                 "price_tier1": 1, "price_tier2": 2}]}),
                        0, 3)
        # 只有一笔 10 件收料：3 分钟 × 10 件 = 30 件分钟
        self.assertEqual(r.details[0].piece_minutes[TIER1], 30)

    def test_resend_with_different_minute_is_breakpoint(self):
        # 同 action_id、同批次但 minute 不同 -> 内容不同，按断点处理
        second = dict(_receive(2, 1))
        second["action_id"] = "R1"
        second["batch"] = "B1"
        flow = [_receive(1, 0), second]
        with self.assertRaises(BillingError):
            run_billing(_write(self.td, "diff.json", flow),
                        _write(self.td, "p.json", {
                            "threshold_minutes": 5, "rates": [
                                {"warehouse": "WH1", "location": "A",
                                 "effective_minute": 0,
                                 "price_tier1": 1, "price_tier2": 2}]}),
                        0, 3)

    def test_billing_replay_matches_canonical_engine_slices(self):
        actions = parse_file(FLOW)
        self.assertFalse(run(actions).has_errors)  # 样例流水本身无断点
        # 能成功跑出账单即说明内部 _snapshot 一致性校验通过
        report = run_billing(FLOW, PRICES, 3, 12)
        self.assertEqual(report.total_fee_micro, 1156)


class TestLargeQuantitiesNotExpanded(unittest.TestCase):
    def test_million_qty_stays_few_spell_objects(self):
        td = tempfile.mkdtemp()
        flow = [
            _receive(1, 0, qty=3_000_000),
            {"seq": 2, "action_id": "I1", "op": "isolate",
             "material": "M1", "warehouse": "WH1", "qty": 1_500_000,
             "from_location": "A", "minute": 100},
            {"seq": 3, "action_id": "REL1", "op": "release",
             "material": "M1", "warehouse": "WH1", "qty": 1_500_000,
             "isolate_id": "I1", "location": "A", "minute": 200},
            {"seq": 4, "action_id": "T1", "op": "transfer",
             "material": "M1", "warehouse": "WH1", "qty": 3_000_000,
             "from_location": "A", "location": "B", "minute": 300}]
        prices = {"threshold_minutes": 50, "rates": [
            {"warehouse": "WH1", "location": "A", "effective_minute": 0,
             "price_tier1": 2, "price_tier2": 3},
            {"warehouse": "WH1", "location": "B", "effective_minute": 0,
             "price_tier1": 1, "price_tier2": 2}]}
        fp, pp = _write(td, "f.json", flow), _write(td, "p.json", prices)
        report = run_billing(fp, pp, 0, 400)
        # 仍是少量分组与整数件分钟，绝不按件/按分钟展开
        self.assertLessEqual(len(report.details), 4)
        actions = parse_file(fp)
        from req_slicer.engine.runner import plan_groups
        from req_slicer.engine.model import MaterialLedger
        ordered = list(plan_groups(actions).values())[0]
        lg = MaterialLedger("M1", "WH1")
        spells = BillingLedger(lg).replay(ordered, horizon=400)
        self.assertLess(len(spells), 12)


class TestCli(unittest.TestCase):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = billing_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_success_emits_json_zero(self):
        code, out, err = self._run("--flow", FLOW, "--prices", PRICES,
                                   "--start", "3", "--end", "12")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["total_fee_micro"], 1156)

    def test_failure_no_bill_exit_one(self):
        with tempfile.TemporaryDirectory() as td:
            bad_prices = _write(td, "p.json", {
                "threshold_minutes": 1, "rates": []})  # 应收费却完全缺价
            code, out, err = self._run("--flow", FLOW, "--prices",
                                       bad_prices, "--start", "3", "--end", "12")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("[错误]", err)

    def test_out_file_written_only_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "bill.json")
            code, out, _ = self._run("--flow", FLOW, "--prices", PRICES,
                                     "--start", "3", "--end", "12",
                                     "--out", target)
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), json.loads(out))

    def test_registered_on_main_cli(self):
        from req_slicer.cli import build_parser, _register_handover
        parser = build_parser()
        _register_handover(parser)
        args = parser.parse_args(
            ["billing", "--flow", FLOW, "--prices", PRICES,
             "--start", "0", "--end", "1"])
        self.assertTrue(hasattr(args, "func"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
