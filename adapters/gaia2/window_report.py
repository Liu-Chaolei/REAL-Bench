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
    unresolved = [
        r
        for r in rows
        if r["result"].get("status") == "completed_unscored"
        and r.get("audit", {}).get("passed") is True
    ]
    summary = {
        "protocol": manifest["protocol"],
        "state": ledger["state"],
        "planned": planned,
        "requested_attempt_limit": ledger.get(
            "requested_attempt_limit", manifest.get("attempt_limit")
        ),
        "unresolved_task_scores": len(unresolved),
        "score_coverage": len(verified) / len(rows) if rows else None,
        "verified_pass_rate": sum(r["result"].get("success") is True for r in verified)
        / len(verified)
        if verified
        else None,
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
        "unverified_or_invalid_results": len(rows) - len(verified) - len(unresolved),
        "score_bases": dict(
            Counter(
                r["result"].get("score_basis", "official_full_judge") for r in verified
            )
        ),
        "unverified_interruption_results": sum(
            r["result"].get("speech_evidence_status") == "unverified_interruption"
            for r in rows
        ),
        "statuses": dict(Counter(r["result"]["status"] for r in rows)),
        "boundary_model_calls": sum(
            r["result"].get("boundary_responses_calls", 0) for r in rows
        ),
        "semantic_interaction_quality": "not_assessed",
        "native_recovery_count": sum(
            (r["result"].get("native_recovery") or {}).get("count", 0) for r in rows
        ),
        "native_recovery_completed": sum(
            (r["result"].get("native_recovery") or {}).get("completed_count", 0)
            for r in rows
        ),
        "native_recovery_sec": sum(
            (r["result"].get("native_recovery") or {}).get("duration_sec", 0)
            for r in rows
        ),
        "failure_origins": dict(
            Counter(
                r["result"].get("failure_origin")
                for r in rows
                if r["result"].get("failure_origin")
            )
        ),
    }
    (campaign / "PROGRESS.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        f"# {manifest['protocol']} 评测进度",
        "",
        f"状态：{summary['state']}；本次上限 {summary['requested_attempt_limit']}；父配置计划 {planned} 题，结束 {len(rows)} 题，中断 {len(interrupted)} 题。",
        f"通过审计的成绩分母 {len(verified)}，其中任务通过 {summary['verified_task_passes']}；未验证或无效 {summary['unverified_or_invalid_results']}。",
        "",
        f"待定 {len(unresolved)} 题；可评分覆盖率 {summary['score_coverage']}。待定不计为 0 分。",
        f"原生恢复：{summary['native_recovery_completed']}/{summary['native_recovery_count']} 次确认就绪，累计 {summary['native_recovery_sec']:.2f} 秒；故障归因 {summary['failure_origins']}。",
        f"评分来源：{summary['score_bases']}；完整评分与官方必要条件失败分别计数。",
        f"无法关联取消证据：{summary['unverified_interruption_results']} 题；这不等于录音文件损坏。",
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
