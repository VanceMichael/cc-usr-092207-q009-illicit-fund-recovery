"""台账查询：内部持有说明、家属公开视图、待决定范围与证据图。"""

from __future__ import annotations

from .model import RECOVERED_STATUSES, BatchStatus, ClaimStatus, LeadStatus, fen_to_yuan

#: 证据图中作为节点呈现的程序性事件
EVENT_NODE_LABELS = {
    "seizure": "扣押",
    "restitution": "退赔",
    "surrender": "上缴",
    "disburse": "发还",
    "court_finding": "法院认定",
    "cash_estimate_correction": "现金估算修正",
}


def holding_report(ledger) -> list[dict]:
    """说明每一元目前由谁持有、依据何在、是否已追缴（内部视图）。

    法院认定与在案主张分别列示，未经证实的说法不与司法确认混同。
    """
    rows = []
    for batch in ledger.live_batches():
        claims = [c for c in ledger.claims.values() if batch.batch_id in c.batches]
        rows.append(
            {
                "批次": batch.batch_id,
                "金额_元": fen_to_yuan(batch.amount),
                "交付方式": batch.form.value,
                "持有人": batch.holder,
                "状态": batch.status.value,
                "依据事件": batch.basis,
                "法院认定": batch.legal_nature,
                "认定依据事件": batch.legal_nature_basis,
                "在案主张": [
                    {"主张": c.claim_id, "性质": c.nature.value, "状态": c.status.value}
                    for c in claims
                ],
                "是否已追缴": batch.status in RECOVERED_STATUSES,
            }
        )
    return sorted(rows, key=lambda r: r["批次"])


def recovery_progress(ledger) -> dict:
    """家属依法可查询的退赔进度：只有金额与状态，不含证言、主张、线索与持有人身份。"""
    summary: dict[str, int] = {}
    for batch in ledger.live_batches():
        key = batch.status.value
        summary[key] = summary.get(key, 0) + fen_to_yuan(batch.amount)
    return {
        "案件": ledger.case_id,
        "当前阶段": ledger.stage.value,
        "登记总额_元": fen_to_yuan(ledger.baseline),
        "状态汇总_元": summary,
        "未追缴_元": summary.get(BatchStatus.HELD.value, 0),
        "批次进度": [
            {"批次": b.batch_id, "金额_元": fen_to_yuan(b.amount), "状态": b.status.value}
            for b in sorted(ledger.live_batches(), key=lambda x: x.batch_id)
        ],
    }


def undecided_ranges(ledger) -> list[dict]:
    """不同法律定性下仍待决定的资金范围：未获法院认定且存在争议的批次。"""
    rows = []
    for batch in ledger.live_batches():
        if batch.legal_nature is not None:
            continue
        claims = [
            c
            for c in ledger.claims.values()
            if batch.batch_id in c.batches
            and c.status in (ClaimStatus.PENDING, ClaimStatus.CORROBORATED)
        ]
        leads = [
            l
            for l in ledger.leads.values()
            if batch.batch_id in l.batches and l.status == LeadStatus.PENDING
        ]
        notes = [n for n in ledger.undecided_notes if batch.batch_id in n["batches"]]
        if not (claims or leads or notes):
            continue
        rows.append(
            {
                "批次": batch.batch_id,
                "金额_元": fen_to_yuan(batch.amount),
                "争议主张": [
                    {"主张": c.claim_id, "性质": c.nature.value, "状态": c.status.value}
                    for c in claims
                ],
                "待核查线索": [l.lead_id for l in leads],
                "法院待定事项": [n["issue"] for n in notes],
                "定性选项": sorted({option for n in notes for option in n["options"]}),
            }
        )
    return sorted(rows, key=lambda r: r["批次"])


def _event_batches(event) -> list[str]:
    p = event.payload
    ids = []
    if "batch" in p:
        ids.append(p["batch"])
    if "from_batch" in p:
        ids.append(p["from_batch"])
    for key in ("restituted", "remainder", "returned"):
        if isinstance(p.get(key), dict):
            ids.append(p[key]["batch"])
    for part in p.get("parts", []):
        ids.append(part["batch"])
    for finding in p.get("findings", []):
        ids.extend(finding.get("batches", []))
    for item in p.get("undecided", []):
        ids.extend(item.get("batches", []))
    return ids


def evidence_graph(ledger) -> dict:
    """导出证据图：资金批次、持有人、主张、证言、凭证、线索与处置事件及其关联。"""
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def node(node_id: str, node_type: str, **attrs) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "type": node_type, **attrs})

    for batch in ledger.batches.values():
        node(
            f"batch:{batch.batch_id}",
            "资金批次",
            金额_元=fen_to_yuan(batch.amount),
            交付方式=batch.form.value,
            状态=batch.status.value,
        )
        if batch.status != BatchStatus.SPLIT:
            node(f"party:{batch.holder}", "持有人")
            edges.append({"from": f"batch:{batch.batch_id}", "to": f"party:{batch.holder}", "type": "现持有人"})
        for parent in batch.parents:
            edges.append({"from": f"batch:{parent}", "to": f"batch:{batch.batch_id}", "type": "拆分自"})
        for voucher in batch.evidence:
            node(f"voucher:{voucher}", "凭证")
            edges.append({"from": f"voucher:{voucher}", "to": f"batch:{batch.batch_id}", "type": "凭证支持"})
        for testimony_id in batch.testimony:
            testimony = ledger.testimonies.get(testimony_id)
            node(
                f"testimony:{testimony_id}",
                "证言",
                已翻转=testimony.reversed if testimony else None,
            )
            edges.append({"from": f"testimony:{testimony_id}", "to": f"batch:{batch.batch_id}", "type": "涉及批次"})

    for claim in ledger.claims.values():
        node(f"claim:{claim.claim_id}", "主张", 性质=claim.nature.value, 状态=claim.status.value)
        node(f"party:{claim.claimant}", "当事人")
        edges.append({"from": f"party:{claim.claimant}", "to": f"claim:{claim.claim_id}", "type": "提出主张"})
        for batch_id in claim.batches:
            edges.append({"from": f"claim:{claim.claim_id}", "to": f"batch:{batch_id}", "type": "主张对象"})
        for testimony_id in claim.testimony:
            testimony = ledger.testimonies.get(testimony_id)
            node(
                f"testimony:{testimony_id}",
                "证言",
                已翻转=testimony.reversed if testimony else None,
            )
            edges.append({"from": f"testimony:{testimony_id}", "to": f"claim:{claim.claim_id}", "type": "证言支持"})
        for document in claim.documents:
            node(f"doc:{document}", "书证")
            edges.append({"from": f"doc:{document}", "to": f"claim:{claim.claim_id}", "type": "书证支持"})
        for voucher in claim.vouchers:
            node(f"voucher:{voucher}", "凭证")
            edges.append({"from": f"voucher:{voucher}", "to": f"claim:{claim.claim_id}", "type": "凭证支持"})

    for lead in ledger.leads.values():
        node(f"lead:{lead.lead_id}", "待核查线索", 状态=lead.status.value)
        for batch_id in lead.batches:
            edges.append({"from": f"lead:{lead.lead_id}", "to": f"batch:{batch_id}", "type": "线索涉及"})
        for voucher in lead.vouchers:
            node(f"voucher:{voucher}", "凭证")
            edges.append({"from": f"voucher:{voucher}", "to": f"lead:{lead.lead_id}", "type": "凭证支持"})

    for event in ledger.events:
        if event.kind not in EVENT_NODE_LABELS:
            continue
        node(f"event:{event.event_id}", EVENT_NODE_LABELS[event.kind], 发生日期=event.occurred_on)
        for batch_id in _event_batches(event):
            edges.append({"from": f"event:{event.event_id}", "to": f"batch:{batch_id}", "type": "处置"})

    return {"nodes": nodes, "edges": edges}
