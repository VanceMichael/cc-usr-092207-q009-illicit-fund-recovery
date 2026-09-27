"""读取示例案件并回放为台账。"""

from __future__ import annotations

import json
from pathlib import Path

from .events import event_from_dict
from .ledger import Ledger
from .model import yuan_to_fen


def load_case(path):
    """返回案件元数据、当事人表与事件列表。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = {"case_id", "reported_total_yuan", "parties", "events"} - set(raw)
    if missing:
        raise ValueError(f"案件资料缺少字段: {sorted(missing)}")
    parties = {p["id"]: p.get("label", p["id"]) for p in raw["parties"]}
    events = [event_from_dict(item) for item in raw["events"]]
    return raw, parties, events


def build_ledger(path) -> Ledger:
    """按事件流重建台账；任一事件越权、越阶段或破坏守恒都会在此暴露。"""
    raw, parties, events = load_case(path)
    ledger = Ledger(
        case_id=raw["case_id"],
        reported_total=yuan_to_fen(raw["reported_total_yuan"]),
        parties=parties,
    )
    for event in events:
        ledger.apply(event)
    return ledger
