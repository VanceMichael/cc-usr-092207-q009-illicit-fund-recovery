"""涉案请托资金追缴协作的领域模型。

金额一律以分为最小单位（整数），避免浮点误差破坏资金守恒。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

FEN_PER_YUAN = 100

#: 持有人待定的占位标识（如现金交付事实未获认定时）
UNKNOWN_HOLDER = "待核查"


def yuan_to_fen(amount_yuan: int) -> int:
    return int(amount_yuan) * FEN_PER_YUAN


def fen_to_yuan(amount_fen: int) -> int:
    if amount_fen % FEN_PER_YUAN != 0:
        raise ValueError("金额无法整除为元")
    return amount_fen // FEN_PER_YUAN


class Form(str, Enum):
    CASH = "现金"
    TRANSFER = "转账"


class BatchStatus(str, Enum):
    HELD = "在持"
    RETURNED = "已退回"
    SEIZED = "已扣押"
    RESTITUTED = "已退赔"
    SURRENDERED = "已上缴"
    SPLIT = "已拆分"


#: 已追缴 = 已在办案机关控制之下、已退赔或已上缴；已退回请托人不计入追缴
RECOVERED_STATUSES = {BatchStatus.SEIZED, BatchStatus.RESTITUTED, BatchStatus.SURRENDERED}


class ClaimNature(str, Enum):
    SOLICITED = "请托款"
    FEE = "报酬"
    REFUND = "退款"
    LOAN = "借款"
    INTERCEPTED = "截留款"


class ClaimStatus(str, Enum):
    PENDING = "待核查"
    CORROBORATED = "部分印证"
    CONFIRMED = "法院认定"
    REJECTED = "不予认定"


class LeadStatus(str, Enum):
    PENDING = "待核查"
    VERIFIED = "已核实"
    CLEARED = "已排除"


class Stage(str, Enum):
    INVESTIGATION = "侦查"
    PROSECUTION = "审查起诉"
    TRIAL = "审判"
    ENFORCEMENT = "执行"


class Role(str, Enum):
    INVESTIGATOR = "侦查人员"
    PROSECUTOR = "检察人员"
    JUDGE = "审判人员"
    CUSTODIAN = "涉案财物管理人员"
    FAMILY = "家属"


@dataclass
class Batch:
    """一笔资金批次；拆分后父批次状态变为已拆分，不再参与守恒合计。"""

    batch_id: str
    amount: int  # 分
    form: Form
    holder: str
    status: BatchStatus
    parents: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # 凭证编号
    testimony: list[str] = field(default_factory=list)  # 证言编号
    amount_status: str = "已核实"  # 或 估算
    legal_nature: str | None = None  # 仅法院认定事件可写入
    legal_nature_basis: str | None = None  # 认定事件编号
    basis: str = ""  # 形成当前持有/状态的事件编号
    history: list[dict] = field(default_factory=list)


@dataclass
class Claim:
    """当事人对某批资金性质的主张；与法院认定分层存放，不得混同。"""

    claim_id: str
    batches: list[str]
    nature: ClaimNature
    claimant: str
    testimony: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)  # 书证（借条等）
    vouchers: list[str] = field(default_factory=list)
    status: ClaimStatus = ClaimStatus.PENDING
    note: str = ""
    history: list[dict] = field(default_factory=list)


@dataclass
class Testimony:
    testimony_id: str
    by: str
    reversed: bool = False
    history: list[dict] = field(default_factory=list)


@dataclass
class Lead:
    lead_id: str
    batches: list[str]
    description: str
    vouchers: list[str] = field(default_factory=list)
    status: LeadStatus = LeadStatus.PENDING
    history: list[dict] = field(default_factory=list)
