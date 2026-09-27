"""涉案资金台账：逐事件入账，强制守恒，全程留版本。"""

from __future__ import annotations

from .access import TRANSITIONS, check_write
from .events import KINDS, Event
from .model import (
    UNKNOWN_HOLDER,
    Batch,
    BatchStatus,
    Claim,
    ClaimNature,
    ClaimStatus,
    Form,
    Lead,
    LeadStatus,
    Stage,
    Testimony,
    fen_to_yuan,
    yuan_to_fen,
)


class EventError(Exception):
    """事件内容不合法。"""


class ConservationError(Exception):
    """资金拆分合并不守恒。"""


class Ledger:
    """以事件流驱动的资金台账。

    - 守恒：在册批次合计恒等于登记基准（交付登记累计 ± 现金估算修正）。
    - 版本：每次变更写入实体历史，台账可按事件序号回放。
    - 分层：主张与法院认定分开存放，认定只能由 court_finding 事件写入。
    """

    def __init__(self, case_id: str = "", reported_total: int = 0, parties: dict | None = None):
        self.case_id = case_id
        self.reported_total = reported_total  # 报案金额（分），仅作参照
        self.parties = dict(parties or {})
        self.stage = Stage.INVESTIGATION
        self.batches: dict[str, Batch] = {}
        self.claims: dict[str, Claim] = {}
        self.testimonies: dict[str, Testimony] = {}
        self.leads: dict[str, Lead] = {}
        self.recovery_targets: list[dict] = []
        self.recovery_target_history: list[dict] = []
        self.stage_history: list[dict] = []
        self.undecided_notes: list[dict] = []
        self.events: list[Event] = []
        self.baseline = 0  # 登记基准：交付登记累计 ± 现金估算修正
        self._event_ids: set[str] = set()

    @property
    def version(self) -> int:
        return len(self.events)

    def live_batches(self) -> list[Batch]:
        return [b for b in self.batches.values() if b.status != BatchStatus.SPLIT]

    def tracked_total(self) -> int:
        return sum(b.amount for b in self.live_batches())

    def apply(self, event: Event) -> None:
        if event.kind not in KINDS:
            raise EventError(f"未知事件类型{event.kind}")
        if event.event_id in self._event_ids:
            raise EventError(f"事件{event.event_id}已登记")
        check_write(event.actor, event.kind, self.stage)
        handler = getattr(self, f"_apply_{event.kind}", None)
        if handler is None:
            raise EventError(f"事件类型{event.kind}未实现")
        handler(event)
        event.seq = len(self.events) + 1
        self.events.append(event)
        self._event_ids.add(event.event_id)
        if self.tracked_total() != self.baseline:
            raise ConservationError(
                f"事件{event.event_id}入账后在册{self.tracked_total()}分"
                f"与登记基准{self.baseline}分不守恒"
            )

    def at_version(self, n: int) -> "Ledger":
        """回放到第 n 个事件之后的台账状态。"""
        if not 0 <= n <= len(self.events):
            raise ValueError("版本序号超出范围")
        clone = Ledger(self.case_id, self.reported_total, self.parties)
        for event in self.events[:n]:
            clone.apply(event)
        return clone

    # ---- 基础校验 ----

    def _rev(self, event: Event, note: str, detail: dict | None = None) -> dict:
        return {
            "event": event.event_id,
            "seq": len(self.events) + 1,
            "note": note,
            "detail": detail or {},
        }

    @staticmethod
    def _require(payload: dict, *keys: str) -> dict:
        missing = [k for k in keys if k not in payload]
        if missing:
            raise EventError(f"事件内容缺少字段: {missing}")
        return payload

    def _check_holder(self, holder: str) -> None:
        if holder != UNKNOWN_HOLDER and holder not in self.parties:
            raise EventError(f"未知当事人{holder}")

    def _live(self, batch_id: str) -> Batch:
        try:
            batch = self.batches[batch_id]
        except KeyError:
            raise EventError(f"未知批次{batch_id}") from None
        if batch.status == BatchStatus.SPLIT:
            raise EventError(f"批次{batch_id}已拆分，不得再操作")
        return batch

    def _register_testimonies(self, items: list[dict] | None, event: Event) -> list[str]:
        items = items or []
        for item in items:
            self._require(item, "id", "by")
            self._check_holder(item["by"])
        ids = []
        for item in items:
            if item["id"] not in self.testimonies:
                self.testimonies[item["id"]] = Testimony(item["id"], item["by"])
            ids.append(item["id"])
        return ids

    # ---- 资金事件 ----

    def _apply_delivery(self, event: Event) -> None:
        p = self._require(event.payload, "batch", "amount_yuan", "form", "from", "to")
        if p["batch"] in self.batches:
            raise EventError(f"批次{p['batch']}已存在")
        self._check_holder(p["from"])
        self._check_holder(p["to"])
        testimony = self._register_testimonies(p.get("testimony"), event)
        batch = Batch(
            batch_id=p["batch"],
            amount=yuan_to_fen(p["amount_yuan"]),
            form=Form(p["form"]),
            holder=p["to"],
            status=BatchStatus.HELD,
            evidence=list(p.get("vouchers", [])),
            testimony=testimony,
            amount_status=p.get("amount_status", "已核实"),
            basis=event.event_id,
        )
        batch.history.append(
            self._rev(event, f"登记交付：{p['from']}→{p['to']}", {"amount_yuan": p["amount_yuan"]})
        )
        self.batches[batch.batch_id] = batch
        self.baseline += batch.amount

    def _create_children(self, event: Event, parent: Batch, specs: list[tuple]) -> list[Batch]:
        """拆分父批次。specs: (批次号, 金额分, 持有人, 状态, 凭证, 证言项, 备注)。

        先完成全部校验再写入，任一子批次不合法则父批次保持原状。
        """
        total = sum(spec[1] for spec in specs)
        if total != parent.amount:
            raise ConservationError(
                f"批次{parent.batch_id}拆分子额合计{total}分与原额{parent.amount}分不守恒"
            )
        ids = [spec[0] for spec in specs]
        if len(set(ids)) != len(ids):
            raise EventError("子批次编号重复")
        for spec in specs:
            if spec[0] in self.batches:
                raise EventError(f"批次{spec[0]}已存在")
            self._check_holder(spec[2])
        children = []
        for batch_id, amount, holder, status, vouchers, testimony_items, note in specs:
            testimony = self._register_testimonies(testimony_items, event)
            child = Batch(
                batch_id=batch_id,
                amount=amount,
                form=parent.form,
                holder=holder,
                status=status,
                parents=[parent.batch_id],
                evidence=list(vouchers),
                testimony=testimony,
                amount_status=parent.amount_status,
                basis=event.event_id,
            )
            child.history.append(
                self._rev(event, note or f"自{parent.batch_id}拆分", {"amount_yuan": fen_to_yuan(amount)})
            )
            self.batches[batch_id] = child
            children.append(child)
        parent.status = BatchStatus.SPLIT
        parent.history.append(self._rev(event, f"拆分为{','.join(ids)}", {"children": ids}))
        self._propagate_split(parent.batch_id, ids, event)
        return children

    def _propagate_split(self, parent_id: str, child_ids: list[str], event: Event) -> None:
        """批次拆分后，指向父批次的主张、线索、追缴对象随之改指子批次。"""
        for claim in self.claims.values():
            if parent_id in claim.batches:
                claim.batches = [b for b in claim.batches if b != parent_id] + list(child_ids)
                claim.history.append(
                    self._rev(event, f"批次{parent_id}拆分，主张范围随之调整", {"batches": claim.batches})
                )
        for lead in self.leads.values():
            if parent_id in lead.batches:
                lead.batches = [b for b in lead.batches if b != parent_id] + list(child_ids)
                lead.history.append(
                    self._rev(event, f"批次{parent_id}拆分，线索范围随之调整", {"batches": lead.batches})
                )
        for target in self.recovery_targets:
            if parent_id in target["batches"]:
                target["batches"] = [b for b in target["batches"] if b != parent_id] + list(child_ids)

    def _part_spec(self, part: dict, status: BatchStatus) -> tuple:
        self._require(part, "batch", "amount_yuan", "to")
        return (
            part["batch"],
            yuan_to_fen(part["amount_yuan"]),
            part["to"],
            status,
            list(part.get("vouchers", [])),
            list(part.get("testimony", [])),
            part.get("note", ""),
        )

    def _remainder_spec(self, part: dict, parent: Batch) -> tuple:
        self._require(part, "batch", "amount_yuan")
        return (
            part["batch"],
            yuan_to_fen(part["amount_yuan"]),
            parent.holder,
            parent.status,
            [],
            [],
            part.get("note", ""),
        )

    def _apply_transfer_onward(self, event: Event) -> None:
        p = self._require(event.payload, "from_batch", "parts")
        parent = self._live(p["from_batch"])
        specs = [self._part_spec(part, BatchStatus.HELD) for part in p["parts"]]
        self._create_children(event, parent, specs)

    def _apply_return_to_payer(self, event: Event) -> None:
        p = self._require(event.payload, "from_batch", "returned")
        parent = self._live(p["from_batch"])
        specs = [self._part_spec(p["returned"], BatchStatus.RETURNED)]
        if "remainder" in p:
            specs.append(self._remainder_spec(p["remainder"], parent))
        self._create_children(event, parent, specs)

    def _apply_restitution(self, event: Event) -> None:
        p = self._require(event.payload, "from_batch", "restituted")
        parent = self._live(p["from_batch"])
        specs = [self._part_spec(p["restituted"], BatchStatus.RESTITUTED)]
        if "remainder" in p:
            specs.append(self._remainder_spec(p["remainder"], parent))
        self._create_children(event, parent, specs)

    def _apply_form_change(self, event: Event) -> None:
        p = self._require(event.payload, "batch", "new_form")
        batch = self._live(p["batch"])
        new_form = Form(p["new_form"])
        if new_form == batch.form:
            raise EventError("形态未发生变化")
        old_form = batch.form
        batch.form = new_form
        batch.evidence.extend(p.get("vouchers", []))
        batch.history.append(
            self._rev(event, p.get("note", "形态变更"), {"old": old_form.value, "new": new_form.value})
        )

    def _apply_cash_estimate_correction(self, event: Event) -> None:
        p = self._require(event.payload, "batch", "new_amount_yuan", "reason")
        batch = self._live(p["batch"])
        if batch.form != Form.CASH:
            raise EventError("仅现金批次存在估算修正")
        new_amount = yuan_to_fen(p["new_amount_yuan"])
        delta = new_amount - batch.amount
        if delta == 0:
            raise EventError("修正前后金额一致")
        testimony = self._register_testimonies(p.get("testimony"), event)
        old_amount = batch.amount
        batch.amount = new_amount
        batch.amount_status = "已核实"
        batch.evidence.extend(p.get("vouchers", []))
        batch.testimony.extend(t for t in testimony if t not in batch.testimony)
        self.baseline += delta
        batch.history.append(
            self._rev(
                event,
                p["reason"],
                {
                    "old_amount_yuan": fen_to_yuan(old_amount),
                    "new_amount_yuan": fen_to_yuan(new_amount),
                    "delta_yuan": fen_to_yuan(delta),
                },
            )
        )

    def _apply_seizure(self, event: Event) -> None:
        p = self._require(event.payload, "batch", "to")
        batch = self._live(p["batch"])
        if batch.status != BatchStatus.HELD:
            raise EventError(f"批次{batch.batch_id}状态为{batch.status.value}，不得扣押")
        self._check_holder(p["to"])
        batch.holder = p["to"]
        batch.status = BatchStatus.SEIZED
        batch.basis = event.event_id
        batch.evidence.extend(p.get("vouchers", []))
        batch.history.append(self._rev(event, p.get("note", "扣押"), {"holder": p["to"]}))

    def _apply_surrender(self, event: Event) -> None:
        p = self._require(event.payload, "batch", "to")
        batch = self._live(p["batch"])
        if batch.status != BatchStatus.SEIZED:
            raise EventError("仅已扣押批次可上缴")
        self._check_holder(p["to"])
        batch.holder = p["to"]
        batch.status = BatchStatus.SURRENDERED
        batch.basis = event.event_id
        batch.history.append(self._rev(event, p.get("note", "上缴"), {"holder": p["to"]}))

    def _apply_disburse(self, event: Event) -> None:
        p = self._require(event.payload, "batch", "to")
        batch = self._live(p["batch"])
        if batch.status != BatchStatus.RESTITUTED:
            raise EventError("仅已退赔批次可发还")
        self._check_holder(p["to"])
        batch.holder = p["to"]
        batch.basis = event.event_id
        batch.history.append(self._rev(event, p.get("note", "退赔发还"), {"holder": p["to"]}))

    # ---- 证据与主张 ----

    def _apply_claim(self, event: Event) -> None:
        p = self._require(event.payload, "claim", "batches", "nature", "claimant")
        if p["claim"] in self.claims:
            raise EventError(f"主张{p['claim']}已存在")
        self._check_holder(p["claimant"])
        for batch_id in p["batches"]:
            self._live(batch_id)
        testimony = self._register_testimonies(p.get("testimony"), event)
        supporting = testimony + list(p.get("documents", [])) + list(p.get("vouchers", []))
        status = ClaimStatus.CORROBORATED if supporting else ClaimStatus.PENDING
        claim = Claim(
            claim_id=p["claim"],
            batches=list(p["batches"]),
            nature=ClaimNature(p["nature"]),
            claimant=p["claimant"],
            testimony=testimony,
            documents=list(p.get("documents", [])),
            vouchers=list(p.get("vouchers", [])),
            status=status,
            note=p.get("note", ""),
        )
        claim.history.append(
            self._rev(event, "登记主张", {"status": status.value, "nature": claim.nature.value})
        )
        self.claims[claim.claim_id] = claim

    def _apply_testimony_reversal(self, event: Event) -> None:
        p = self._require(event.payload, "testimony", "reason")
        testimony = self.testimonies.get(p["testimony"])
        if testimony is None:
            raise EventError(f"未知证言{p['testimony']}")
        if testimony.reversed:
            raise EventError(f"证言{p['testimony']}已翻转")
        testimony.reversed = True
        testimony.history.append(self._rev(event, p["reason"], {}))
        # 仅靠该证言支撑的主张支持坍塌，降回待核查；法院已认定的主张不受影响
        for claim in self.claims.values():
            if p["testimony"] not in claim.testimony:
                continue
            if claim.status not in (ClaimStatus.PENDING, ClaimStatus.CORROBORATED):
                continue
            supporting = [t for t in claim.testimony if not self.testimonies[t].reversed]
            supporting += claim.documents + claim.vouchers
            new_status = ClaimStatus.CORROBORATED if supporting else ClaimStatus.PENDING
            if new_status != claim.status:
                claim.status = new_status
                claim.history.append(
                    self._rev(event, f"证言{p['testimony']}翻转，主张支持坍塌", {"status": new_status.value})
                )

    def _apply_lead(self, event: Event) -> None:
        p = self._require(event.payload, "lead", "batches", "description")
        if p["lead"] in self.leads:
            raise EventError(f"线索{p['lead']}已存在")
        for batch_id in p["batches"]:
            if batch_id not in self.batches:
                raise EventError(f"未知批次{batch_id}")
        lead = Lead(p["lead"], list(p["batches"]), p["description"], list(p.get("vouchers", [])))
        lead.history.append(self._rev(event, "登记线索", {}))
        self.leads[lead.lead_id] = lead

    # ---- 程序性事件 ----

    def _apply_case_transfer(self, event: Event) -> None:
        p = self._require(event.payload, "to_stage")
        to_stage = Stage(p["to_stage"])
        expect = TRANSITIONS.get(self.stage)
        if expect != to_stage:
            raise EventError(f"案件不得由{self.stage.value}移送至{to_stage.value}")
        self.stage_history.append(self._rev(event, f"{self.stage.value}→{to_stage.value}", {}))
        self.stage = to_stage

    def _apply_recovery_target_change(self, event: Event) -> None:
        p = self._require(event.payload, "targets")
        targets = []
        for item in p["targets"]:
            self._require(item, "party", "amount_yuan")
            self._check_holder(item["party"])
            for batch_id in item.get("batches", []):
                if batch_id not in self.batches:
                    raise EventError(f"未知批次{batch_id}")
            targets.append(
                {
                    "party": item["party"],
                    "amount": yuan_to_fen(item["amount_yuan"]),
                    "batches": list(item.get("batches", [])),
                    "basis": item.get("basis", ""),
                }
            )
        self.recovery_targets = targets
        self.recovery_target_history.append(
            self._rev(
                event,
                p.get("note", "追缴对象调整"),
                {"targets": [{"party": t["party"], "amount_yuan": fen_to_yuan(t["amount"])} for t in targets]},
            )
        )

    def _apply_court_finding(self, event: Event) -> None:
        p = self._require(event.payload, "findings")
        # 先整体校验，再写入，避免认定落到一半
        for finding in p["findings"]:
            for batch_id in finding.get("batches", []):
                self._live(batch_id)
            if "holder" in finding:
                self._check_holder(finding["holder"])
        for claim_id in p.get("confirm_claims", []) + p.get("reject_claims", []):
            if claim_id not in self.claims:
                raise EventError(f"未知主张{claim_id}")
        for item in p.get("resolve_leads", []):
            if item.get("lead") not in self.leads:
                raise EventError(f"未知线索{item.get('lead')}")
        for item in p.get("undecided", []):
            for batch_id in item.get("batches", []):
                if batch_id not in self.batches:
                    raise EventError(f"未知批次{batch_id}")
        for finding in p["findings"]:
            for batch_id in finding.get("batches", []):
                batch = self.batches[batch_id]
                if "legal_nature" in finding:
                    batch.legal_nature = finding["legal_nature"]
                    batch.legal_nature_basis = event.event_id
                if "holder" in finding:
                    batch.holder = finding["holder"]
                    batch.basis = event.event_id
                batch.history.append(
                    self._rev(
                        event,
                        finding.get("note", "法院认定"),
                        {"legal_nature": finding.get("legal_nature"), "holder": finding.get("holder")},
                    )
                )
        for claim_id in p.get("confirm_claims", []):
            self._set_claim_status(claim_id, ClaimStatus.CONFIRMED, event, "法院认定主张成立")
        for claim_id in p.get("reject_claims", []):
            self._set_claim_status(claim_id, ClaimStatus.REJECTED, event, "法院不予认定")
        for item in p.get("resolve_leads", []):
            lead = self.leads[item["lead"]]
            lead.status = LeadStatus(item["status"])
            lead.history.append(self._rev(event, item.get("note", "线索核查结论"), {"status": lead.status.value}))
        for item in p.get("undecided", []):
            self.undecided_notes.append(
                {
                    "event": event.event_id,
                    "batches": list(item["batches"]),
                    "issue": item["issue"],
                    "options": list(item.get("options", [])),
                    "note": item.get("note", ""),
                }
            )

    def _set_claim_status(self, claim_id: str, status: ClaimStatus, event: Event, note: str) -> None:
        claim = self.claims[claim_id]
        claim.status = status
        claim.history.append(self._rev(event, note, {"status": status.value}))
