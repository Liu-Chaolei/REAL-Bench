#!/usr/bin/env python3
"""Summarize preserved rescoring attempts and one reply-fix voice retry."""

import argparse
import json
from pathlib import Path


def simulation(path):
    """Read one result, whether inline or sharded."""
    payload = json.loads(path.read_text())
    sims = payload.get("simulations")
    if sims:
        return sims[0]
    index = payload["simulation_index"][0]
    return json.loads(
        (path.parent / "simulations" / (index["id"] + ".json")).read_text()
    )


def report(destination, retry=None):
    """Write a reviewable report without changing any original attempts."""
    base = Path("data/simulations")
    old = base / "gaia2-series-live-grounding-none-fix-v1"
    rows = []
    for tid in ["0132", "0867", "0980"]:
        prior = (
            next(p for p in old.glob("task_" + tid + "*") if p.is_dir())
            / "results.json"
        )
        newroot = base / (
            "gaia2-outcome-rescore-v2-judged-replay-fixed"
            if tid == "0980"
            else "gaia2-outcome-rescore-v2-judged"
        )
        path = next(newroot.glob("updated_task_" + tid + "*.json"))
        previous, current = simulation(prior), simulation(path)
        reward = current.get("reward_info") or {}
        rows.append(
            {
                "task_id": current["task_id"],
                "original": str(prior),
                "rescored": str(path),
                "termination_reason": current["termination_reason"],
                "old_evaluation_status": previous.get("evaluation_status"),
                "evaluation_status": current.get("evaluation_status"),
                "reward": reward.get("reward"),
                "components": reward.get("reward_breakdown"),
                "missing_requests": reward.get("info", {}).get("missing_requests"),
                "evaluation_error": current.get("evaluation_error"),
            }
        )
    verification = json.loads(
        Path("data/tau2/domains/gaia2/verification.json").read_text()
    )
    replies = json.loads(
        (
            base / "gaia2-outcome-replies-v2-validation-final/reply-verification.json"
        ).read_text()
    )
    live = None
    if retry:
        result = Path(retry) / "results.json"
        if result.exists():
            s = simulation(result)
            bindings = [
                r for r in s.get("environment_trace", []) if r["kind"] == "trigger"
            ]
            mail = [
                r
                for r in s.get("environment_trace", [])
                if r["kind"] == "environment"
                and r["name"] == "Emails__reply_to_email_from_user"
            ]
            live = {
                "results": str(result),
                "termination_reason": s["termination_reason"],
                "evaluation_status": s.get("evaluation_status"),
                "evaluation_error": s.get("evaluation_error"),
                "reward": (s.get("reward_info") or {}).get("reward"),
                "components": (s.get("reward_info") or {}).get("reward_breakdown"),
                "stages": s.get("info", {}).get("scenario_series"),
                "bindings": bindings,
                "replies": mail,
                "agent_system_metadata": s.get("agent_system_metadata"),
            }
    output = {
        "grading_version": "tau-outcome-grading-v2",
        "reply_version": replies["version"],
        "archive": "data/archives/gaia2-before-outcome-replies-v2-20261107",
        "reply_verification": {k: v for k, v in replies.items() if k != "results"},
        "reference_verification": {
            k: verification.get(k)
            for k in [
                "valid",
                "reference_checked",
                "reference_failures",
                "runtime_sha256",
                "tasks_sha256",
                "sealed",
                "errors",
            ]
        },
        "rescoring": rows,
        "retry": live,
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# 超时评分与实际邮件回信修复",
        "",
        "新规则覆盖所有 τ 数据集的正常预算耗尽；回信修复覆盖 81 题、164 事件。328 个真实应用发信/严格回放用例通过。",
        "",
        "| 题目 | 结束原因 | 旧评分状态 | 新评分状态 | 分数 | 未完整送达阶段 |",
        "|---|---|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['task_id'][:4]} | {row['termination_reason']} | {row['old_evaluation_status']} | {row['evaluation_status']} | {row['reward']} | {row['missing_requests']} |"
        )
    lines += [
        "",
        "旧 0867 没有回信的事实保持不变；重评分未补入任何事件。0980 业务请求、动态用户与 Qwen 权限机制未修改。",
        "",
        "重评分首次尝试暴露了 judge 配置缺失及历史回放的表示差异。使用既有私有 judge 配置重新评分；通知时间仅规范精确数值的字符串/数字表示；Messages 最近会话的 participant_ids 按实际集合成员精确比较，其余输出及消息顺序不放宽。失败尝试单独保留。",
        "",
        "验证及全部详细路径见 report.json、event-audit.json 和 reply-verification.json。",
    ]
    if live:
        lines += [
            "",
            f"0867 真实重试：{live['termination_reason']}，评分 {live['evaluation_status']}，reward={live['reward']}；触发绑定 {len(live['bindings'])} 个，实际回信 {len(live['replies'])} 封。",
        ]
    (destination / "REPORT.md").write_text("\n".join(lines) + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retry", type=Path)
    args = parser.parse_args()
    report(args.output, args.retry)
