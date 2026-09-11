"""Objective v4 batch metrics; no semantic labels or additional judge calls."""

from __future__ import annotations

import json
from collections import Counter


def write_report(campaign, ledger, manifest):
    rows = ledger.get("results", [])
    verified = [
        r
        for r in rows
        if r["result"].get("measurement_valid") is True
        and r.get("audit", {}).get("passed") is True
    ]
    active = (ledger.get("active") or {}).get("cases", [])
    interrupted = ledger.get("interrupted", [])
    planned = len(manifest["seed_cases"])
    summary = {
        "protocol": manifest["protocol"],
        "state": ledger["state"],
        "planned": planned,
        "original_selection_count": len(manifest.get("original_selection_ids", [])),
        "supported": sum(
            r.get("protocol_support", {}).get("supported") is True
            for r in manifest["seed_cases"]
        ),
        "attempted": len(rows) + len(interrupted) + len(active),
        "ended": len(rows),
        "active": active,
        "interrupted": len(interrupted),
        "not_started": planned - len(rows) - len(interrupted) - len(active),
        "verified_score_denominator": len(verified),
        "verified_task_passes": sum(
            r["result"].get("success") is True for r in verified
        ),
        "unverified_or_invalid_results": len(rows) - len(verified),
        "statuses": dict(Counter(r["result"]["status"] for r in rows)),
        "boundary_model_calls": sum(
            r["result"].get("boundary_responses_calls", 0) for r in rows
        ),
        "semantic_interaction_quality": "not_assessed",
    }
    (campaign / "PROGRESS.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        "# V4 固定观察窗口进度",
        "",
        f"状态：{summary['state']}；计划 {planned} 题，结束 {len(rows)} 题，中断 {len(interrupted)} 题。",
        f"通过审计的成绩分母 {len(verified)}，其中任务通过 {summary['verified_task_passes']}；未验证或无效 {summary['unverified_or_invalid_results']}。",
        "",
        "最后实际播报时间不代表已确认的最终答案；不执行额外交互语义评分。",
        "",
        "| 题目 | 运行结果 | 官方结果 | 审计 | 输入秒数 | 首次操作秒数 | 最后播报秒数 | 观察结束原因 |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def value(number):
        return "缺失" if number is None else f"{number:.2f}"

    for entry in rows:
        result = entry["result"]
        timing = result.get("timing", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    entry["source_id"][:4],
                    result["status"],
                    str(result.get("success")),
                    str(entry.get("audit", {}).get("passed")),
                    value(timing.get("input_audio_sec")),
                    value(timing.get("first_application_action_sec")),
                    value(timing.get("last_audio_playback_end_sec")),
                    str(timing.get("observation_end_reason")),
                ]
            )
            + " |"
        )
    (campaign / "PROGRESS.md").write_text("\n".join(lines) + "\n")
    return summary
