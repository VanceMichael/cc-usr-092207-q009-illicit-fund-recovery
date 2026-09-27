"""台账事件：每一次资金或证据状态变化都是一条不可改写的记录。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Role

KINDS = {
    "delivery",  # 交付登记（资金进入台账）
    "transfer_onward",  # 转交拆分
    "return_to_payer",  # 退回请托人
    "form_change",  # 现金/转账形态变更（取现、存入）
    "cash_estimate_correction",  # 现金估算修正
    "claim",  # 主张登记（报酬、退款、借款等说法）
    "testimony_reversal",  # 证言翻转
    "lead",  # 待核查线索登记
    "seizure",  # 扣押/冻结
    "restitution",  # 退赔（可部分）
    "surrender",  # 上缴国库
    "disburse",  # 退赔发还
    "case_transfer",  # 案件移送（阶段流转）
    "recovery_target_change",  # 追缴对象变化
    "court_finding",  # 法院认定
}


@dataclass
class Event:
    event_id: str
    kind: str
    actor: Role
    occurred_on: str
    recorded_on: str
    payload: dict = field(default_factory=dict)
    note: str = ""
    seq: int = 0


def event_from_dict(data: dict) -> Event:
    missing = {"id", "kind", "actor", "occurred_on", "recorded_on", "payload"} - set(data)
    if missing:
        raise ValueError(f"事件缺少字段: {sorted(missing)}")
    if data["kind"] not in KINDS:
        raise ValueError(f"未知事件类型: {data['kind']}")
    return Event(
        event_id=data["id"],
        kind=data["kind"],
        actor=Role(data["actor"]),
        occurred_on=data["occurred_on"],
        recorded_on=data["recorded_on"],
        payload=data["payload"],
        note=data.get("note", ""),
    )
