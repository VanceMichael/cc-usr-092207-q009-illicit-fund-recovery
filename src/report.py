"""命令行报告：说明每一元目前由谁持有、依据何在、是否已追缴。

用法：
    python -m src.report
"""

import argparse
import json
from pathlib import Path

from .access import AccessControl
from .domain import load_and_validate
from .ledger import Ledger

STATUS_LABEL = {
    "outstanding": "尚未追缴（个人持有）",
    "returned": "诉前退回",
    "seized_pending": "已扣押、待处置",
    "restituted": "判决退赔",
    "forfeited": "没收上缴国库",
}


def build_report(path: Path) -> dict:
    data = load_and_validate(path)
    ledger = Ledger(data)
    access = AccessControl(data, ledger)

    per_yuan = []
    for leaf in ledger.leaves():
        trace = ledger.trace(leaf.batch_id)
        per_yuan.append(
            {
                "batch": leaf.batch_id,
                "amount": leaf.amount,
                "holder": trace["holder_label"],
                "status": STATUS_LABEL[leaf.status],
                "recovered": trace["recovered"],
                "certainty": leaf.certainty,
                "path": " → ".join(trace["path"]),
                "basis_evidence": trace["evidence"],
                "judicial_findings": trace["judicial_findings"],
                "unproven_claims": trace["unproven_claims"],
            }
        )

    return {
        "case": data["case"],
        "genesis_total": ledger.genesis_total(),
        "leaf_total": ledger.leaf_total(),
        "conservation_ok": ledger.genesis_total() == ledger.leaf_total(),
        "recovery_summary": {
            k: v for k, v in ledger.recovery_summary().items()
        },
        "per_batch": per_yuan,
        "scenarios": [
            ledger.scenario_report(s["id"]) for s in data["scenarios"]
        ],
        "open_leads": [l["summary"] for l in data["leads"] if l["status"] == "open"],
        "revisions": [
            {"id": r["id"], "kind": r["kind"], "reason": r["reason"]}
            for r in data["revisions"]
        ],
        "family_public_view": access.view("ACT-FAM"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="涉案请托资金逐元报告")
    parser.add_argument(
        "--path", default="fixtures/domain.json", help="共享资料路径"
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出"
    )
    args = parser.parse_args()

    report = build_report(Path(args.path))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f"案件：{report['case']['id']}（当前阶段 {report['case']['current_stage']}）")
    print(
        f"初始入账 {report['genesis_total']:,} 元，"
        f"末梢合计 {report['leaf_total']:,} 元，"
        f"守恒：{'是' if report['conservation_ok'] else '否'}"
    )
    print("\n每一元的现状：")
    for row in report["per_batch"]:
        print(
            f"  {row['batch']}  {row['amount']:>10,} 元  "
            f"{row['holder']}｜{row['status']}｜"
            f"{'已追缴' if row['recovered'] else '未追缴'}｜"
            f"路径 {row['path']}"
        )
    summary = report["recovery_summary"]
    print(
        "\n追缴汇总："
        f"已追缴 {summary['recovered']:,} 元"
        f"（退回 {summary['returned']:,} + 退赔 {summary['restituted']:,} "
        f"+ 没收 {summary['forfeited']:,}）；"
        f"待追缴 {summary['pending_recovery']:,} 元"
        f"（个人持有 {summary['outstanding']:,} + 扣押待处置 {summary['seized_pending']:,}）"
    )
    print("\n不同法律定性下仍待决定的范围：")
    for scenario in report["scenarios"]:
        print(
            f"  {scenario['label']}：违法所得待追缴 {scenario['illicit_pending_recovery']:,} 元，"
            f"性质争议待定 {scenario['disputed_pending_decision']:,} 元"
        )
    print("\n留痕版本：")
    for revision in report["revisions"]:
        print(f"  {revision['id']} {revision['kind']}：{revision['reason']}")
    print(f"\n待核查线索（未计入金额）：{len(report['open_leads'])} 条")


if __name__ == "__main__":
    main()
