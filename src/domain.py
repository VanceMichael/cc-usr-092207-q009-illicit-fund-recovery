"""读取并检查共享的领域资料。

校验分两层：``load_domain`` 负责顶层字段的最低要求；``validate_domain``
负责证据图内部的引用完整性与守恒前置规则。任何一层失败都抛出
``ValueError``，保证进入账本计算的资料一定自洽。
"""

import json
from pathlib import Path

REQUIRED_TOP_LEVEL = {
    "domain",
    "version",
    "sample_id",
    "unit_of_account",
    "actors",
    "holders",
    "evidence",
    "fund_batches",
    "events",
    "claims",
    "findings",
    "revisions",
    "case_transfers",
    "recovery_targets",
    "leads",
    "scenarios",
    "restitution_progress",
    "case",
    "facts",
    "constraints",
}

ACTOR_KINDS = {"investigator", "prosecutor", "judge", "custodian", "family"}
HOLDER_KINDS = {"person", "authority"}
EVENT_KINDS = {"deliver", "return", "intercept", "seize", "restitution"}
DELIVERIES = {"cash", "transfer", "seizure", "restitution", "forfeiture"}
CERTAINTIES = {"claimed", "evidenced", "confirmed", "derived"}
EVIDENCE_KINDS = {
    "transfer_voucher",
    "cash_estimate",
    "withdrawal_record",
    "statement_record",
    "iou_document",
    "seizure_record",
    "restitution_receipt",
    "forfeiture_order",
}
REVISION_KINDS = {
    "cash_estimate_correction",
    "testimony_reversal",
    "case_transfer",
    "recovery_target_change",
    "partial_restitution",
}
DISPOSITION_KINDS = {"restitution", "forfeiture"}
FINDING_ISSUERS = {"prosecutor", "judge"}
SCENARIO_BUCKETS = {"illicit", "disputed", "excluded"}
STAGES = {"investigation", "prosecution", "trial", "enforcement"}


def load_domain(path: Path) -> dict:
    """返回字段完整且带版本的业务资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not REQUIRED_TOP_LEVEL.issubset(value):
        raise ValueError("共享资料缺少必要字段")
    if value["version"] < 1 or len(value["actors"]) < 2 or len(value["facts"]) < 2 or len(value["constraints"]) < 2:
        raise ValueError("共享资料内容不完整")
    return value


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def _check_unique(problems: list[str], rows: list[dict], label: str) -> set[str]:
    seen: set[str] = set()
    for row in rows:
        rid = row.get("id")
        if rid in seen:
            _fail(problems, f"{label} 编号重复：{rid}")
        seen.add(rid)
    return seen


def validate_domain(value: dict) -> list[str]:
    """返回证据图的全部校验问题，空列表表示资料自洽。"""
    problems: list[str] = []

    actors = value.get("actors", [])
    holders = value.get("holders", [])
    evidence = value.get("evidence", [])
    batches = value.get("fund_batches", [])
    events = value.get("events", [])
    claims = value.get("claims", [])
    findings = value.get("findings", [])
    revisions = value.get("revisions", [])
    transfers = value.get("case_transfers", [])
    targets = value.get("recovery_targets", [])
    leads = value.get("leads", [])
    scenarios = value.get("scenarios", [])
    progress = value.get("restitution_progress", [])
    case = value.get("case", {})

    actor_ids = _check_unique(problems, actors, "参与方")
    holder_ids = _check_unique(problems, holders, "持有人")
    evidence_ids = _check_unique(problems, evidence, "凭证")
    batch_ids = _check_unique(problems, batches, "资金批次")
    event_ids = _check_unique(problems, events, "流转事件")
    claim_ids = _check_unique(problems, claims, "主张")
    finding_ids = _check_unique(problems, findings, "司法认定")
    revision_ids = _check_unique(problems, revisions, "版本记录")
    transfer_ids = _check_unique(problems, transfers, "案件移送")
    target_ids = _check_unique(problems, targets, "追缴对象")
    _check_unique(problems, leads, "待核查线索")
    _check_unique(problems, scenarios, "定性情形")
    _check_unique(problems, progress, "退赔进度")

    actor_kinds = {a["id"]: a.get("kind") for a in actors}
    for actor in actors:
        if actor.get("kind") not in ACTOR_KINDS:
            _fail(problems, f"参与方 {actor.get('id')} 类型非法")
    for holder in holders:
        if holder.get("kind") not in HOLDER_KINDS:
            _fail(problems, f"持有人 {holder.get('id')} 类型非法")

    batch_amount = {b["id"]: b.get("amount") for b in batches}
    batch_created_by = {b["id"]: b.get("created_by") for b in batches}
    batch_created_on = {b["id"]: b.get("created_on") for b in batches}
    event_date = {e["id"]: e.get("date") for e in events}

    for batch in batches:
        if batch.get("delivery") not in DELIVERIES:
            _fail(problems, f"批次 {batch['id']} 交付方式非法")
        if batch.get("certainty") not in CERTAINTIES:
            _fail(problems, f"批次 {batch['id']} 可认定程度非法")
        if not isinstance(batch.get("amount"), int) or batch["amount"] <= 0:
            _fail(problems, f"批次 {batch['id']} 金额必须为正整数")

    for item in evidence:
        if item.get("kind") not in EVIDENCE_KINDS:
            _fail(problems, f"凭证 {item['id']} 类型非法")
        for correction in item.get("corrections", []):
            if correction.get("revision_id") not in revision_ids:
                _fail(problems, f"凭证 {item['id']} 的修正缺少对应版本记录")

    # 每个批次至多被一个事件消耗，且累计消耗不得超过批次金额。
    consumed: dict[str, int] = {}
    for event in events:
        eid = event.get("id")
        if event.get("kind") not in EVENT_KINDS:
            _fail(problems, f"事件 {eid} 类型非法")
        if event.get("delivery") not in DELIVERIES:
            _fail(problems, f"事件 {eid} 交付方式非法")
        if event.get("certainty") not in CERTAINTIES:
            _fail(problems, f"事件 {eid} 可认定程度非法")
        if event.get("from_holder") not in holder_ids or event.get("to_holder") not in holder_ids:
            _fail(problems, f"事件 {eid} 的持有人引用无效")
        for ref in event.get("evidence", []):
            if ref not in evidence_ids:
                _fail(problems, f"事件 {eid} 引用了不存在的凭证 {ref}")
        in_sum = 0
        for entry in event.get("inputs", []):
            bid = entry.get("batch")
            if bid not in batch_ids:
                _fail(problems, f"事件 {eid} 消耗了不存在的批次 {bid}")
                continue
            if entry.get("amount", 0) <= 0:
                _fail(problems, f"事件 {eid} 消耗金额必须为正")
            consumed[bid] = consumed.get(bid, 0) + entry["amount"]
            in_sum += entry["amount"]
        out_sum = 0
        for entry in event.get("outputs", []):
            bid = entry.get("batch")
            if bid not in batch_ids:
                _fail(problems, f"事件 {eid} 产出了未登记的批次 {bid}")
            else:
                if batch_created_by.get(bid) != eid:
                    _fail(problems, f"批次 {bid} 的创建事件与 {eid} 不一致")
                if batch_amount.get(bid) != entry.get("amount"):
                    _fail(problems, f"批次 {bid} 登记金额与事件产出金额不一致")
                if batch_created_on.get(bid) != event.get("date"):
                    _fail(problems, f"批次 {bid} 的成立日期与事件日期不一致")
            if entry.get("holder") not in holder_ids:
                _fail(problems, f"事件 {eid} 的产出持有人引用无效")
            out_sum += entry.get("amount", 0)
        if in_sum != out_sum and event.get("inputs"):
            _fail(problems, f"事件 {eid} 不守恒：消耗 {in_sum}，产出 {out_sum}")
        if not event.get("inputs"):
            if event.get("kind") != "deliver":
                _fail(problems, f"事件 {eid} 无来源批次，只有交付事件允许凭空入账")
            if not event.get("outputs"):
                _fail(problems, f"事件 {eid} 无来源批次时至少要产出一个批次")
        if event.get("kind") in {"seize", "restitution"} and event.get("certainty") != "confirmed":
            _fail(problems, f"事件 {eid} 属扣押或退赔，必须以司法确认等级入账")

    for bid, total in consumed.items():
        if total > batch_amount.get(bid, 0):
            _fail(problems, f"批次 {bid} 被超额消耗")

    # 时序：批次只能被晚于其成立日期的事件消耗。
    for event in events:
        for entry in event.get("inputs", []):
            bid = entry.get("batch")
            if bid in batch_created_on and event.get("date", "") < batch_created_on[bid]:
                _fail(problems, f"事件 {event['id']} 早于批次 {bid} 的成立日期")

    for claim in claims:
        if claim.get("asserter") not in holder_ids:
            _fail(problems, f"主张 {claim['id']} 的主张人引用无效")
        for ref in claim.get("about", []):
            if ref not in event_ids:
                _fail(problems, f"主张 {claim['id']} 指向了不存在的事件 {ref}")
        for ref in claim.get("evidence", []):
            if ref not in evidence_ids:
                _fail(problems, f"主张 {claim['id']} 引用了不存在的凭证 {ref}")
        history = claim.get("history", [])
        versions = [h.get("version") for h in history]
        if versions != sorted(versions) or len(set(versions)) != len(versions):
            _fail(problems, f"主张 {claim['id']} 的版本链不连续")
        if history and claim.get("version") != versions[-1]:
            _fail(problems, f"主张 {claim['id']} 的当前版本与历史末版不一致")
        if history and history[-1].get("nature") != claim.get("nature"):
            _fail(problems, f"主张 {claim['id']} 的当前性质与历史末版不一致")

    for finding in findings:
        if actor_kinds.get(finding.get("issued_by")) not in FINDING_ISSUERS:
            _fail(problems, f"司法认定 {finding['id']} 只能由检察或审判主体作出")
        if not finding.get("events"):
            _fail(problems, f"司法认定 {finding['id']} 必须关联至少一个流转事件")
        for ref in finding.get("events", []):
            if ref not in event_ids:
                _fail(problems, f"司法认定 {finding['id']} 指向了不存在的事件 {ref}")
        for ref in finding.get("evidence", []):
            if ref not in evidence_ids:
                _fail(problems, f"司法认定 {finding['id']} 引用了不存在的凭证 {ref}")
        for disposition in finding.get("dispositions", []):
            if disposition.get("kind") not in DISPOSITION_KINDS:
                _fail(problems, f"司法认定 {finding['id']} 的处置方式非法")
            if disposition.get("event") not in event_ids:
                _fail(problems, f"司法认定 {finding['id']} 的处置事件引用无效")

    valid_revision_refs = (
        evidence_ids | claim_ids | event_ids | transfer_ids | target_ids | finding_ids
    )
    for revision in revisions:
        if revision.get("kind") not in REVISION_KINDS:
            _fail(problems, f"版本记录 {revision['id']} 类型非法")
        for ref in revision.get("refs", []):
            if ref not in valid_revision_refs:
                _fail(problems, f"版本记录 {revision['id']} 引用了不存在的对象 {ref}")

    for transfer in transfers:
        if transfer.get("revision_id") not in revision_ids:
            _fail(problems, f"案件移送 {transfer['id']} 缺少对应版本记录")

    for target in targets:
        if target.get("holder") not in holder_ids:
            _fail(problems, f"追缴对象 {target['id']} 的持有人引用无效")
        if target.get("status") == "active" and target.get("until") is not None:
            _fail(problems, f"追缴对象 {target['id']} 仍在追缴中，不应有截止日期")
        if target.get("status") == "closed" and target.get("until") is None:
            _fail(problems, f"追缴对象 {target['id']} 已结束，必须留痕截止日期")
        if target.get("revision_id") is not None and target.get("revision_id") not in revision_ids:
            _fail(problems, f"追缴对象 {target['id']} 的版本记录引用无效")

    for lead in leads:
        if lead.get("status") not in {"open", "closed"}:
            _fail(problems, f"待核查线索 {lead['id']} 状态非法")

    for scenario in scenarios:
        for classification in scenario.get("classifications", []):
            if classification.get("batch") not in batch_ids:
                _fail(problems, f"情形 {scenario['id']} 分类了不存在的批次")
            if classification.get("bucket") not in SCENARIO_BUCKETS:
                _fail(problems, f"情形 {scenario['id']} 的分类取值非法")

    if case.get("current_stage") not in STAGES:
        _fail(problems, "案件当前阶段非法")

    return problems


def load_and_validate(path: Path) -> dict:
    """读取资料并通过全部校验，失败时抛出 ValueError。"""
    value = load_domain(path)
    problems = validate_domain(value)
    if problems:
        raise ValueError("共享资料未通过校验：" + "；".join(problems))
    return value
