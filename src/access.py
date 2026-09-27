"""按角色与案件阶段控制可见范围。

- 侦查、检察、审判人员按当前案件阶段工作：只能在自己的阶段写入，全程可读图。
- 涉案财物管理人员只接触持有、扣押、退赔与没收的执行信息。
- 家属只能查询依法可公开的退赔进度，看不到证言、主张、待核查线索和其他持有人。
"""

from __future__ import annotations

from .ledger import Ledger

STAGE_ORDER = ["investigation", "prosecution", "trial", "enforcement"]

# 各角色可在哪个阶段写入证据图
WRITE_STAGE = {
    "investigator": "investigation",
    "prosecutor": "prosecution",
    "judge": "trial",
    "custodian": "enforcement",
}

# 财物管理人员可见的事件类型
CUSTODIAN_EVENTS = {"seize", "restitution"}
# 财物管理人员可见的凭证类型
CUSTODIAN_EVIDENCE = {"seizure_record", "restitution_receipt", "forfeiture_order"}


class PermissionError_(PermissionError):
    """越权访问时抛出。"""


class AccessControl:
    def __init__(self, data: dict, ledger: Ledger | None = None):
        self.data = data
        self.ledger = ledger or Ledger(data)
        self._actors = {a["id"]: a for a in data["actors"]}

    # ---- 权限判定 ----

    def _actor(self, actor_id: str) -> dict:
        if actor_id not in self._actors:
            raise PermissionError_(f"未知的访问主体：{actor_id}")
        return self._actors[actor_id]

    def can_read_full_graph(self, actor_id: str) -> bool:
        return self._actor(actor_id)["kind"] in {"investigator", "prosecutor", "judge"}

    def can_write(self, actor_id: str) -> bool:
        actor = self._actor(actor_id)
        if actor["kind"] == "family":
            return False
        return WRITE_STAGE.get(actor["kind"]) == self.data["case"]["current_stage"]

    # ---- 视图投影 ----

    def view(self, actor_id: str) -> dict:
        """返回该主体依法可见的投影，内部人员为全图，其他角色为裁剪视图。"""
        kind = self._actor(actor_id)["kind"]
        if kind in {"investigator", "prosecutor", "judge"}:
            return self._internal_view(actor_id)
        if kind == "custodian":
            return self._custodian_view()
        if kind == "family":
            return self._family_view()
        raise PermissionError_(f"角色 {kind} 未配置视图")

    def _internal_view(self, actor_id: str) -> dict:
        summary = self.ledger.recovery_summary()
        return {
            "scope": "full",
            "actor": actor_id,
            "stage": self.data["case"]["current_stage"],
            "can_write": self.can_write(actor_id),
            "case": self.data["case"],
            "holdings": self.ledger.current_holdings(),
            "recovery": summary,
            "leaves": [vars(leaf) for leaf in self.ledger.leaves()],
            "claims_grouped_by_event": self.ledger.claim_groups(),
            "revisions": self.data["revisions"],
            "case_transfers": self.data["case_transfers"],
            "recovery_targets": self.data["recovery_targets"],
            "leads": self.data["leads"],
            "scenarios": [
                self.ledger.scenario_report(s["id"]) for s in self.data["scenarios"]
            ],
        }

    def _custodian_view(self) -> dict:
        events = [
            {
                "id": e["id"],
                "date": e["date"],
                "kind": e["kind"],
                "amounts_in": sum(i["amount"] for i in e["inputs"]),
                "amounts_out": sum(o["amount"] for o in e["outputs"]),
                "from_holder": e["from_holder"],
                "to_holder": e["to_holder"],
                "evidence": e["evidence"],
            }
            for e in self.data["events"]
            if e["kind"] in CUSTODIAN_EVENTS
        ]
        evidence = [
            {"id": v["id"], "kind": v["kind"], "amount": v["amount"], "issued_on": v["issued_on"]}
            for v in self.data["evidence"]
            if v["kind"] in CUSTODIAN_EVIDENCE
        ]
        holdings = {
            holder: amount
            for holder, amount in self.ledger.current_holdings().items()
            if self.data["holders"] and self._is_authority(holder)
        }
        return {
            "scope": "custody",
            "case": self.data["case"]["id"],
            "holdings_in_custody": holdings,
            "events": events,
            "evidence": evidence,
        }

    def _family_view(self) -> dict:
        """依法可公开的退赔进度：只见金额、日期、渠道与公开说明。"""
        return {
            "scope": "public-restitution",
            "case": self.data["case"]["id"],
            "stage": self.data["case"]["current_stage"],
            "restitution_progress": [
                {
                    "date": entry["date"],
                    "amount": entry["amount"],
                    "channel": entry["channel"],
                    "public_note": entry["public_note"],
                }
                for entry in self.data["restitution_progress"]
            ],
            "total_restituted": sum(e["amount"] for e in self.data["restitution_progress"]),
        }

    # ---- 内部工具 ----

    def _is_authority(self, holder_id: str) -> bool:
        return next(h["kind"] == "authority" for h in self.data["holders"] if h["id"] == holder_id)
