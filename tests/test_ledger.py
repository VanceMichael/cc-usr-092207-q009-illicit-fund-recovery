import json
import unittest
from pathlib import Path

from src.access import check_write
from src.case import build_ledger
from src.events import event_from_dict
from src.ledger import ConservationError, EventError
from src.model import BatchStatus, ClaimStatus, Role, Stage, fen_to_yuan, yuan_to_fen
from src.report import evidence_graph, holding_report, recovery_progress, undecided_ranges

FIXTURE = Path("fixtures/case.json")


def make_event(event_id, kind, actor, payload):
    return event_from_dict(
        {
            "id": event_id,
            "kind": kind,
            "actor": actor,
            "occurred_on": "2021-01-01",
            "recorded_on": "2021-01-01",
            "payload": payload,
        }
    )


class FixtureTest(unittest.TestCase):
    """示例案件回放后的整体状态。"""

    @classmethod
    def setUpClass(cls):
        cls.ledger = build_ledger(FIXTURE)

    def test_conservation_holds(self):
        self.assertEqual(self.ledger.tracked_total(), self.ledger.baseline)
        self.assertEqual(fen_to_yuan(self.ledger.baseline), 3_000_000)
        self.assertEqual(self.ledger.version, 21)

    def test_every_yuan_accounted(self):
        rows = holding_report(self.ledger)
        self.assertEqual(sum(row["金额_元"] for row in rows), 3_000_000)
        for row in rows:
            self.assertTrue(row["持有人"])
            self.assertTrue(row["依据事件"])
            self.assertIn("是否已追缴", row)

    def test_correction_keeps_versions(self):
        b1 = self.ledger.batches["B1"]
        corrections = [h for h in b1.history if "delta_yuan" in h["detail"]]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["event"], "E03")
        self.assertEqual(corrections[0]["detail"]["old_amount_yuan"], 1_180_000)
        self.assertEqual(corrections[0]["detail"]["new_amount_yuan"], 1_200_000)

    def test_replay_to_pre_correction_version(self):
        earlier = self.ledger.at_version(2)
        self.assertEqual(fen_to_yuan(earlier.batches["B1"].amount), 1_180_000)
        self.assertEqual(fen_to_yuan(earlier.baseline), 2_980_000)

    def test_case_transfer_leaves_stage_history(self):
        self.assertEqual(
            [h["note"] for h in self.ledger.stage_history],
            ["侦查→审查起诉", "审查起诉→审判", "审判→执行"],
        )
        self.assertEqual(self.ledger.stage, Stage.ENFORCEMENT)

    def test_recovery_target_change_keeps_versions(self):
        self.assertEqual(len(self.ledger.recovery_target_history), 1)
        targets = {t["party"]: t["amount"] for t in self.ledger.recovery_targets}
        self.assertEqual(fen_to_yuan(targets["P-YI"]), 800_000)
        # 退赔拆分后，追缴对象中的批次引用随之指向子批次
        batches = {b for t in self.ledger.recovery_targets for b in t["batches"]}
        self.assertIn("B2b2-t", batches)
        self.assertIn("B2b2-s", batches)

    def test_partial_restitution_splits_and_conserves(self):
        restituted = self.ledger.batches["B2b2-t"]
        remaining = self.ledger.batches["B2b2-s"]
        self.assertEqual(restituted.amount + remaining.amount, yuan_to_fen(300_000))
        self.assertEqual(restituted.status, BatchStatus.RESTITUTED)
        self.assertEqual(remaining.status, BatchStatus.SEIZED)
        self.assertEqual(restituted.holder, "P-JIA")  # 已发还甲

    def test_testimony_reversal_downgrades_claim(self):
        claim = self.ledger.claims["C-02"]
        statuses = [h["detail"].get("status") for h in claim.history]
        self.assertEqual(statuses, ["部分印证", "待核查", "不予认定"])
        self.assertTrue(self.ledger.testimonies["T-BING-01"].reversed)
        self.assertEqual(claim.status, ClaimStatus.REJECTED)

    def test_claims_follow_batch_splits(self):
        self.assertEqual(set(self.ledger.claims["C-01"].batches), {"B2b2-t", "B2b2-s"})

    def test_findings_never_written_by_claims(self):
        # 批次的法律性质只能来自法院认定事件
        for batch in self.ledger.batches.values():
            if batch.legal_nature is not None:
                self.assertEqual(batch.legal_nature_basis, "E18")
        # B1a 交付事实未获认定，持有人回到待核查
        b1a = self.ledger.batches["B1a"]
        self.assertIsNone(b1a.legal_nature)
        self.assertEqual(b1a.holder, "待核查")

    def test_family_view_only_shows_public_progress(self):
        view = recovery_progress(self.ledger)
        self.assertEqual(view["状态汇总_元"]["已退赔"], 120_000)
        self.assertEqual(view["状态汇总_元"]["已退回"], 500_000)
        self.assertEqual(view["状态汇总_元"]["已上缴"], 1_000_000)
        self.assertEqual(view["未追缴_元"], 1_200_000)
        blob = json.dumps(view, ensure_ascii=False)
        for forbidden in ("T-BING-01", "T-YI-03", "报酬", "借款", "主张", "证言", "P-YI", "P-BING", "L-01"):
            self.assertNotIn(forbidden, blob)

    def test_undecided_ranges(self):
        rows = undecided_ranges(self.ledger)
        self.assertEqual([row["批次"] for row in rows], ["B1a"])
        self.assertEqual(rows[0]["金额_元"], 700_000)
        self.assertEqual(len(rows[0]["定性选项"]), 2)

    def test_evidence_graph_covers_required_types(self):
        graph = evidence_graph(self.ledger)
        types = {node["type"] for node in graph["nodes"]}
        for required in ("资金批次", "持有人", "主张", "证言", "凭证", "书证", "待核查线索", "扣押", "退赔", "法院认定"):
            self.assertIn(required, types)
        for node in graph["nodes"]:
            if node["type"] == "资金批次":
                self.assertIn(node["交付方式"], ("现金", "转账"))


class PermissionTest(unittest.TestCase):
    """阶段权限：越角色、越阶段、家属写入一律拒绝。"""

    @classmethod
    def setUpClass(cls):
        cls.ledger = build_ledger(FIXTURE)

    def test_investigator_blocked_after_transfer(self):
        event = make_event(
            "X01",
            "delivery",
            "侦查人员",
            {"batch": "Z9", "amount_yuan": 1000, "form": "现金", "from": "P-JIA", "to": "P-YI"},
        )
        with self.assertRaises(PermissionError):
            self.ledger.apply(event)  # 案件已至执行阶段

    def test_family_has_no_write_access(self):
        with self.assertRaises(PermissionError):
            check_write(Role.FAMILY, "seizure", Stage.INVESTIGATION)

    def test_prosecutor_blocked_outside_stage(self):
        with self.assertRaises(PermissionError):
            check_write(Role.PROSECUTOR, "recovery_target_change", Stage.INVESTIGATION)

    def test_custodian_can_record_restitution_during_prosecution(self):
        replayed = self.ledger.at_version(15)  # 审查起诉阶段，E16 尚未入账
        replayed.apply(self.ledger.events[15])  # E16 部分退赔
        self.assertEqual(replayed.version, 16)
        self.assertEqual(replayed.tracked_total(), replayed.baseline)

    def test_invalid_transfer_direction_rejected(self):
        replayed = self.ledger.at_version(2)
        event = make_event("X02", "case_transfer", "侦查人员", {"to_stage": "审判"})
        with self.assertRaises(EventError):
            replayed.apply(event)


class ConservationTest(unittest.TestCase):
    """拆分合并必须守恒，失败的事件不得留下痕迹。"""

    @classmethod
    def setUpClass(cls):
        cls.ledger = build_ledger(FIXTURE)

    def test_unequal_split_rejected(self):
        replayed = self.ledger.at_version(3)  # B1 已修正为120万元、尚未拆分
        event = make_event(
            "X03",
            "transfer_onward",
            "侦查人员",
            {
                "from_batch": "B1",
                "parts": [
                    {"batch": "Z1", "amount_yuan": 700000, "to": "P-BING"},
                    {"batch": "Z2", "amount_yuan": 400000, "to": "P-YI"},
                ],
            },
        )
        with self.assertRaises(ConservationError):
            replayed.apply(event)
        self.assertNotIn("Z1", replayed.batches)
        self.assertEqual(replayed.batches["B1"].status, BatchStatus.HELD)

    def test_equal_split_accepted(self):
        replayed = self.ledger.at_version(3)
        event = make_event(
            "X04",
            "transfer_onward",
            "侦查人员",
            {
                "from_batch": "B1",
                "parts": [
                    {"batch": "Z1", "amount_yuan": 700000, "to": "P-BING"},
                    {"batch": "Z2", "amount_yuan": 500000, "to": "P-YI"},
                ],
            },
        )
        replayed.apply(event)
        self.assertEqual(replayed.tracked_total(), replayed.baseline)
        self.assertEqual(replayed.batches["B1"].status, BatchStatus.SPLIT)

    def test_correction_requires_delta(self):
        replayed = self.ledger.at_version(2)
        event = make_event(
            "X05",
            "cash_estimate_correction",
            "侦查人员",
            {"batch": "B1", "new_amount_yuan": 1180000, "reason": "无变化"},
        )
        with self.assertRaises(EventError):
            replayed.apply(event)

    def test_unknown_holder_rejected(self):
        replayed = self.ledger.at_version(2)
        event = make_event(
            "X06",
            "delivery",
            "侦查人员",
            {"batch": "Z8", "amount_yuan": 1000, "form": "转账", "from": "P-JIA", "to": "P-NOPE"},
        )
        with self.assertRaises(EventError):
            replayed.apply(event)


if __name__ == "__main__":
    unittest.main()
