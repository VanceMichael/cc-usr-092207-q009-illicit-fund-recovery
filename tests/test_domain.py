import copy
import json
import unittest
from pathlib import Path

from src.domain import load_domain, validate_domain, load_and_validate
from src.ledger import (
    Ledger,
    OUTSTANDING,
    RETURNED,
    SEIZED_PENDING,
    RESTITUTED,
    FORFEITED,
)
from src.access import AccessControl
from src.report import build_report

FIXTURE = Path("fixtures/domain.json")


def data():
    return load_and_validate(FIXTURE)


class FixtureTest(unittest.TestCase):
    def test_fixture_matches_domain(self):
        value = load_domain(FIXTURE)
        self.assertEqual(value["domain"], "illicit-fund-recovery")
        self.assertGreaterEqual(value["version"], 2)
        self.assertGreaterEqual(len(value["constraints"]), 2)

    def test_fixture_passes_full_validation(self):
        self.assertEqual(validate_domain(data()), [])


class ConservationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(data())

    def test_genesis_equals_leaf_total(self):
        # 凭空入账总额（300万）必须与此刻仍实际存在的末梢批次总额一致
        self.assertEqual(self.ledger.genesis_total(), 3_000_000)
        self.assertEqual(self.ledger.leaf_total(), 3_000_000)

    def test_holdings_sum_to_genesis(self):
        self.assertEqual(sum(self.ledger.current_holdings().values()), 3_000_000)

    def test_recovery_buckets_partition_total(self):
        summary = self.ledger.recovery_summary()
        partition = (
            summary[OUTSTANDING]
            + summary[RETURNED]
            + summary[SEIZED_PENDING]
            + summary[RESTITUTED]
            + summary[FORFEITED]
        )
        self.assertEqual(partition, 3_000_000)
        self.assertEqual(
            summary["recovered"] + summary["pending_recovery"],
            3_000_000,
        )

    def test_expected_holdings(self):
        holdings = self.ledger.current_holdings()
        # 甲收回160万（诉前60万+判决退赔100万）、国库40万、警方待处置10万、乙仍持90万
        self.assertEqual(holdings["H-JIA"], 1_600_000)
        self.assertEqual(holdings["H-TRE"], 400_000)
        self.assertEqual(holdings["H-POL"], 100_000)
        self.assertEqual(holdings["H-YI"], 900_000)

    def test_claims_are_never_summed(self):
        # 同一笔60万退款被分别描述为退款与（相邻批次）借款，账本仍只有一笔
        groups = self.ledger.claim_groups()
        natures = {c["nature"] for c in groups["E-07"]}
        self.assertIn("借款", natures)
        self.assertIn("退款", natures)
        self.assertIn("居间报酬", natures)
        self.assertEqual(self.ledger.leaf_total(), 3_000_000)

    def test_leads_excluded_from_ledger(self):
        # 待核查线索不产生批次，不进入持有与追缴汇总
        for leaf in self.ledger.leaves():
            self.assertNotIn("亲属账户", leaf.batch_id)
        self.assertEqual(self.ledger.outstanding_for_targets().get("H-YI"), 900_000)

    def test_unbalanced_event_rejected(self):
        bad = data()
        # 破坏一个有来源批次的事件（E-03：200万进，210万出）
        target = next(e for e in bad["events"] if e["id"] == "E-03")
        target["outputs"][0]["amount"] = 1_700_000
        problems = validate_domain(bad)
        self.assertTrue(any("不守恒" in p for p in problems))

    def test_over_consumed_batch_rejected(self):
        bad = data()
        # 让同一批现金被两个事件重复全额消耗，模拟按口供重复建表
        bad["events"][3]["inputs"] = [{"batch": "B-02", "amount": 400_000}]
        problems = validate_domain(bad)
        self.assertTrue(any("超额消耗" in p for p in problems))

    def test_cash_estimate_correction_preserves_amount(self):
        # 修正记录在案，入账金额以修正后的100万为准，而非估算的120万
        voucher = next(v for v in data()["evidence"] if v["id"] == "V-02")
        correction = voucher["corrections"][0]
        self.assertEqual(correction["from_amount"], 1_200_000)
        self.assertEqual(correction["to_amount"], 1_000_000)
        # 初始入账中不存在120万的批次
        self.assertEqual(self.ledger.genesis_total(), 3_000_000)


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(data())

    def test_trace_explains_every_yuan(self):
        for leaf in self.ledger.leaves():
            view = self.ledger.trace(leaf.batch_id)
            self.assertTrue(view["holder_label"])
            self.assertTrue(view["path"])
            self.assertIn(view["status"], {
                OUTSTANDING, RETURNED, SEIZED_PENDING, RESTITUTED, FORFEITED
            })

    def test_trace_separates_claims_from_findings(self):
        view = self.ledger.trace("B-08")  # 借条包装的40万
        self.assertEqual(view["holder"], "H-YI")
        self.assertEqual(view["status"], OUTSTANDING)
        # 该款曾流经丙（E-03→E-06），故 F-1、F-2 均在其血缘上；
        # 关键是未经证实的借款主张与司法认定分列，绝不混同
        self.assertIn("C-3", view["unproven_claims"])
        self.assertIn("C-4", view["unproven_claims"])
        self.assertIn("F-2", view["judicial_findings"])

    def test_lineage_covers_merge_branches(self):
        events = {step["event"] for step in self.ledger.lineage("B-12")}
        # B-12 由丙线100万扣押款执行而来，合并的转账线与现金线都应可溯
        self.assertIn("E-01", events)
        self.assertIn("E-04", events)
        self.assertIn("E-08", events)
        self.assertIn("E-10", events)

    def test_trace_consumed_batch_raises(self):
        with self.assertRaises(KeyError):
            self.ledger.trace("B-01")


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(data())

    def test_scam_scenario_all_illicit(self):
        report = self.ledger.scenario_report("S-1")
        self.assertEqual(report["illicit_pending_recovery"], 1_000_000)
        self.assertEqual(report["disputed_pending_decision"], 0)

    def test_bribery_scenario_has_disputed_slice(self):
        report = self.ledger.scenario_report("S-2")
        self.assertEqual(report["illicit_pending_recovery"], 600_000)
        self.assertEqual(report["disputed_pending_decision"], 400_000)
        # 已决金额不随定性变化
        self.assertEqual(report["already_recovered"], 2_000_000)


class VersioningTest(unittest.TestCase):
    def test_testimony_reversal_keeps_both_versions(self):
        claim = next(c for c in data()["claims"] if c["id"] == "C-2")
        self.assertEqual(claim["version"], 2)
        self.assertEqual(len(claim["history"]), 2)
        self.assertEqual(claim["history"][0]["version"], 1)

    def test_broken_version_chain_rejected(self):
        bad = data()
        bad["claims"][1]["version"] = 3  # 与历史末版不一致
        self.assertTrue(validate_domain(bad))

    def test_transfer_and_target_change_revisioned(self):
        good = data()
        revisions = {r["kind"] for r in good["revisions"]}
        self.assertIn("case_transfer", revisions)
        self.assertIn("recovery_target_change", revisions)
        self.assertIn("partial_restitution", revisions)

    def test_dangling_revision_ref_rejected(self):
        bad = data()
        bad["revisions"][0]["refs"] = ["NO-SUCH"]
        self.assertTrue(any("不存在" in p for p in validate_domain(bad)))


class JudicialConfirmationTest(unittest.TestCase):
    def test_finding_only_by_procuratorate_or_court(self):
        bad = data()
        bad["findings"][0]["issued_by"] = "ACT-INV"
        self.assertTrue(any("司法认定" in p for p in validate_domain(bad)))

    def test_seizure_must_be_confirmed(self):
        bad = data()
        bad["events"][7]["certainty"] = "claimed"
        problems = validate_domain(bad)
        self.assertTrue(any("司法确认" in p for p in problems))

    def test_finding_dispositions_match_events(self):
        finding = next(f for f in data()["findings"] if f["id"] == "F-1")
        amounts = {d["kind"]: d["amount"] for d in finding["dispositions"]}
        self.assertEqual(amounts["restitution"], 600_000)
        self.assertEqual(amounts["forfeiture"], 400_000)


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.ac = AccessControl(data())

    def test_stage_gates_writes(self):
        # 当前处于执行阶段：仅财物管理人员可写，侦查与审判只读
        self.assertTrue(self.ac.can_write("ACT-CUS"))
        self.assertFalse(self.ac.can_write("ACT-INV"))
        self.assertFalse(self.ac.can_write("ACT-JUD"))
        self.assertFalse(self.ac.can_write("ACT-FAM"))

    def test_family_sees_only_public_restitution(self):
        view = self.ac.view("ACT-FAM")
        self.assertEqual(view["scope"], "public-restitution")
        self.assertEqual(view["total_restituted"], 1_600_000)
        serialized = json.dumps(view, ensure_ascii=False)
        for hidden in ("证言", "借条", "待核查", "亲属", "H-YI", "claims", "leads"):
            self.assertNotIn(hidden, serialized)

    def test_internal_view_contains_leads_and_scenarios(self):
        view = self.ac.view("ACT-JUD")
        self.assertEqual(view["scope"], "full")
        self.assertTrue(view["leads"])
        self.assertEqual(len(view["scenarios"]), 2)

    def test_unknown_actor_rejected(self):
        with self.assertRaises(PermissionError):
            self.ac.view("ACT-NOBODY")

    def test_custodian_sees_only_enforcement_events(self):
        view = self.ac.view("ACT-CUS")
        self.assertEqual(view["scope"], "custody")
        kinds = {e["kind"] for e in view["events"]}
        self.assertEqual(kinds, {"seize", "restitution"})


class ReportTest(unittest.TestCase):
    def test_report_accounts_for_every_yuan(self):
        report = build_report(FIXTURE)
        self.assertTrue(report["conservation_ok"])
        self.assertEqual(report["genesis_total"], 3_000_000)
        self.assertEqual(sum(row["amount"] for row in report["per_batch"]), 3_000_000)
        self.assertEqual(len(report["per_batch"]), 7)
        for row in report["per_batch"]:
            self.assertTrue(row["holder"])
            self.assertTrue(row["basis_evidence"])

    def test_report_keeps_leads_separate(self):
        report = build_report(FIXTURE)
        self.assertEqual(len(report["open_leads"]), 2)
        batches_text = json.dumps(report["per_batch"], ensure_ascii=False)
        self.assertNotIn("亲属账户", batches_text)


if __name__ == "__main__":
    unittest.main()
