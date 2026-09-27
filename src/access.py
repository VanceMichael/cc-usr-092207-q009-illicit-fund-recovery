"""按诉讼阶段划分的写入权限；家属仅有依法公开信息的只读视图。"""

from __future__ import annotations

from .model import Role, Stage

INVESTIGATOR_KINDS = {
    "delivery",
    "transfer_onward",
    "return_to_payer",
    "form_change",
    "cash_estimate_correction",
    "claim",
    "testimony_reversal",
    "lead",
    "seizure",
    "case_transfer",
}
PROSECUTOR_KINDS = {"recovery_target_change", "case_transfer", "lead"}
JUDGE_KINDS = {"court_finding", "case_transfer"}
CUSTODIAN_KINDS = {"restitution", "surrender", "disburse"}

#: 各角色可登记的事件类型
WRITE_MATRIX = {
    Role.INVESTIGATOR: INVESTIGATOR_KINDS,
    Role.PROSECUTOR: PROSECUTOR_KINDS,
    Role.JUDGE: JUDGE_KINDS,
    Role.CUSTODIAN: CUSTODIAN_KINDS,
    Role.FAMILY: set(),
}

#: 各角色可操作的诉讼阶段
STAGE_LIMIT = {
    Role.INVESTIGATOR: {Stage.INVESTIGATION},
    Role.PROSECUTOR: {Stage.PROSECUTION},
    Role.JUDGE: {Stage.TRIAL, Stage.ENFORCEMENT},
    Role.CUSTODIAN: set(Stage),
    Role.FAMILY: set(),
}

#: 案件移送的合法路径：当前阶段 -> 下一阶段
TRANSITIONS = {
    Stage.INVESTIGATION: Stage.PROSECUTION,
    Stage.PROSECUTION: Stage.TRIAL,
    Stage.TRIAL: Stage.ENFORCEMENT,
}


def check_write(role: Role, kind: str, stage: Stage) -> None:
    """越权或越阶段登记一律拒绝。"""
    if kind not in WRITE_MATRIX.get(role, set()):
        raise PermissionError(f"{role.value}无权登记{kind}类事件")
    if stage not in STAGE_LIMIT.get(role, set()):
        raise PermissionError(f"案件已处于{stage.value}阶段，{role.value}不得再登记{kind}类事件")
