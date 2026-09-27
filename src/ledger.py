"""证据图之上的守恒账本与逐元追溯。

账本只对**资金批次**求和，永远不对主张（claims）求和——同一笔钱被不同
当事人说成报酬、退款或借款时，账本仍只有一笔，避免按口供分别建表导致的
重复计算。主张与司法认定作为批次的"依据"附着在流转路径上，分层展示。

核心不变量（任何已通过校验的资料都成立）：

    凭空入账总额（初始交付） == 末梢批次总额 == 当前持有人持有总额
"""

from dataclasses import dataclass, field

# 末梢款项的处置状态
OUTSTANDING = "outstanding"          # 仍由个人持有、尚未追缴
RETURNED = "returned"                # 诉前退回请托人
SEIZED_PENDING = "seized_pending"    # 已扣押、待处置
RESTITUTED = "restituted"            # 经司法程序退赔
FORFEITED = "forfeited"              # 没收上缴国库

RECOVERED_STATUSES = {RETURNED, RESTITUTED, FORFEITED}


@dataclass
class Leaf:
    """一个末梢批次：尚未被后续事件消耗、此刻仍实际存在的一笔钱。"""

    batch_id: str
    amount: int
    holder_id: str
    delivery: str
    certainty: str
    created_event: str
    status: str
    evidence: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    path: list[str] = field(default_factory=list)


class Ledger:
    def __init__(self, data: dict):
        self.data = data
        self._events = {e["id"]: e for e in data["events"]}
        self._batches = {b["id"]: b for b in data["fund_batches"]}
        self._holders = {h["id"]: h for h in data["holders"]}
        self._evidence = {v["id"]: v for v in data["evidence"]}
        self._claims = data["claims"]
        self._findings = data["findings"]

        self._consumed: dict[str, int] = {}
        for event in data["events"]:
            for entry in event["inputs"]:
                self._consumed[entry["batch"]] = self._consumed.get(entry["batch"], 0) + entry["amount"]

        self._producer: dict[str, str] = {
            out["batch"]: event["id"]
            for event in data["events"]
            for out in event["outputs"]
        }
        self._leaves = {leaf.batch_id: leaf for leaf in self._build_leaves()}

    # ---- 基本总额 ----

    def genesis_total(self) -> int:
        """凭空入账的初始资金总额（本案应为三百万元）。"""
        return sum(
            out["amount"]
            for event in self.data["events"]
            if not event["inputs"]
            for out in event["outputs"]
        )

    def leaf_total(self) -> int:
        return sum(leaf.amount for leaf in self._leaves.values())

    def current_holdings(self) -> dict[str, int]:
        """每个持有人当前实际持有的金额（含国库与扣押账户）。"""
        totals: dict[str, int] = {}
        for leaf in self._leaves.values():
            totals[leaf.holder_id] = totals.get(leaf.holder_id, 0) + leaf.amount
        return totals

    def leaves(self) -> list[Leaf]:
        return sorted(self._leaves.values(), key=lambda leaf: leaf.batch_id)

    def leaf(self, batch_id: str) -> Leaf:
        return self._leaves[batch_id]

    # ---- 逐元追溯 ----

    def trace(self, batch_id: str) -> dict:
        """说明一个批次当前由谁持有、依据何在、是否已追缴。"""
        leaf = self._leaves.get(batch_id)
        if leaf is None:
            raise KeyError(f"批次 {batch_id} 已被后续事件全部消耗，不是末梢批次")
        return {
            "batch": leaf.batch_id,
            "amount": leaf.amount,
            "holder": leaf.holder_id,
            "holder_label": self._holders[leaf.holder_id]["label"],
            "status": leaf.status,
            "recovered": leaf.status in RECOVERED_STATUSES,
            "certainty": leaf.certainty,
            "path": leaf.path,
            "evidence": leaf.evidence,
            "judicial_findings": leaf.findings,
            "unproven_claims": leaf.claims,
        }

    def lineage(self, batch_id: str) -> list[dict]:
        """返回末梢批次从初始交付起的完整流转路径（事件、对手方、金额）。

        合并事件有多个上游时，全部上游路径都会展开。
        """
        chain: list[dict] = []
        seen_batches: set[str] = set()
        seen_events: set[str] = set()

        def walk(current: str) -> None:
            if current in seen_batches or current not in self._producer:
                return
            seen_batches.add(current)
            event = self._events[self._producer[current]]
            for entry in event["inputs"]:
                walk(entry["batch"])
            if event["id"] in seen_events:
                return
            seen_events.add(event["id"])
            chain.append(
                {
                    "event": event["id"],
                    "date": event["date"],
                    "kind": event["kind"],
                    "delivery": event["delivery"],
                    "from_holder": event["from_holder"],
                    "to_holder": event["to_holder"],
                    "batch_out": current,
                    "certainty": event["certainty"],
                }
            )

        walk(batch_id)
        return sorted(chain, key=lambda step: (step["date"], step["event"]))

    # ---- 追缴汇总（永远只按批次金额求和）----

    def recovery_summary(self) -> dict:
        buckets = {
            OUTSTANDING: 0,
            RETURNED: 0,
            SEIZED_PENDING: 0,
            RESTITUTED: 0,
            FORFEITED: 0,
        }
        for leaf in self._leaves.values():
            buckets[leaf.status] += leaf.amount
        buckets["total"] = self.genesis_total()
        buckets["recovered"] = buckets[RETURNED] + buckets[RESTITUTED] + buckets[FORFEITED]
        buckets["pending_recovery"] = buckets[OUTSTANDING] + buckets[SEIZED_PENDING]
        return buckets

    def outstanding_for_targets(self) -> dict[str, int]:
        """按当前在册追缴对象汇总仍未追缴的金额。"""
        result = {}
        active = {t["holder"] for t in self.data["recovery_targets"] if t["status"] == "active"}
        for leaf in self._leaves.values():
            if leaf.status == OUTSTANDING and leaf.holder_id in active:
                result[leaf.holder_id] = result.get(leaf.holder_id, 0) + leaf.amount
        return result

    # ---- 法律定性情形测算 ----

    def scenario_report(self, scenario_id: str) -> dict:
        """在某一定性情形下，已决范围与仍待决定范围分别是多少。

        情形只对"性质待定"的末梢批次做分类（illicit/disputed/excluded），
        已退赔、已没收等司法确认金额不随情形变化。
        """
        scenario = next(s for s in self.data["scenarios"] if s["id"] == scenario_id)
        buckets = {"illicit": 0, "disputed": 0, "excluded": 0}
        detail = []
        for item in scenario["classifications"]:
            leaf = self._leaves[item["batch"]]
            buckets[item["bucket"]] += leaf.amount
            detail.append(
                {
                    "batch": leaf.batch_id,
                    "amount": leaf.amount,
                    "holder": leaf.holder_id,
                    "status": leaf.status,
                    "bucket": item["bucket"],
                }
            )
        summary = self.recovery_summary()
        return {
            "scenario": scenario["id"],
            "label": scenario["label"],
            "note": scenario["note"],
            "detail": detail,
            "illicit_pending_recovery": buckets["illicit"],
            "disputed_pending_decision": buckets["disputed"],
            "excluded_from_recovery": buckets["excluded"],
            "already_recovered": summary["recovered"],
            "seized_pending": summary[SEIZED_PENDING],
        }

    # ---- 主张分组：同笔钱的多种说法并列，绝不并入金额 ----

    def claim_groups(self) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for claim in self._claims:
            for event_id in claim["about"]:
                groups.setdefault(event_id, []).append(
                    {
                        "claim": claim["id"],
                        "asserter": claim["asserter"],
                        "nature": claim["nature"],
                        "version": claim["version"],
                        "evidence": claim["evidence"],
                    }
                )
        return groups

    # ---- 内部构造 ----

    def _build_leaves(self) -> list[Leaf]:
        leaves: list[Leaf] = []
        for batch in self.data["fund_batches"]:
            bid = batch["id"]
            remaining = batch["amount"] - self._consumed.get(bid, 0)
            if remaining <= 0:
                continue
            if remaining != batch["amount"]:
                # 部分消耗在模型中必须显式拆批：要么全量流转，要么先拆后转。
                raise ValueError(f"批次 {bid} 被部分消耗 {batch['amount'] - remaining} 元，须显式拆分")
            event = self._events[batch["created_by"]]
            holder = next(out["holder"] for out in event["outputs"] if out["batch"] == bid)
            status = self._classify_leaf(batch, event, holder)
            evidence, findings, claims, path = self._basis(bid, event)
            leaves.append(
                Leaf(
                    batch_id=bid,
                    amount=batch["amount"],
                    holder_id=holder,
                    delivery=batch["delivery"],
                    certainty=batch["certainty"],
                    created_event=event["id"],
                    status=status,
                    evidence=evidence,
                    findings=findings,
                    claims=claims,
                    path=path,
                )
            )
        return leaves

    def _classify_leaf(self, batch: dict, event: dict, holder: str) -> str:
        holder_kind = self._holders[holder]["kind"]
        if batch["delivery"] == "forfeiture" or holder == "H-TRE":
            return FORFEITED
        if batch["delivery"] == "restitution":
            return RESTITUTED
        if batch["delivery"] == "seizure" or holder_kind == "authority":
            return SEIZED_PENDING
        if event["kind"] == "return" and holder == event["to_holder"]:
            return RETURNED
        return OUTSTANDING

    def _basis(self, leaf_batch_id: str, producer_event: dict) -> tuple[list[str], list[str], list[str], list[str]]:
        """沿流转路径收集凭证、司法认定、未经证实的主张（含合并事件全部分支）。"""
        event_ids: set[str] = set()
        seen_batches: set[str] = set()

        def walk(current: str) -> None:
            if current in seen_batches or current not in self._producer:
                return
            seen_batches.add(current)
            event = self._events[self._producer[current]]
            for entry in event["inputs"]:
                walk(entry["batch"])
            event_ids.add(event["id"])

        walk(leaf_batch_id)

        ordered = sorted(event_ids, key=lambda eid: (self._events[eid]["date"], eid))
        evidence = sorted({ref for eid in ordered for ref in self._events[eid]["evidence"]})
        findings = sorted(
            finding["id"]
            for finding in self._findings
            if any(eid in finding["events"] for eid in ordered)
        )
        claims = sorted(
            claim["id"]
            for claim in self._claims
            if any(eid in claim["about"] for eid in ordered)
        )
        return evidence, findings, claims, ordered
