"""Unified, model-free voice measurements from classified media events."""

from collections import defaultdict
from math import fsum
from statistics import mean

from tau2.config import VOICE_INTERACTION_CONFIG as CONFIG
from tau2.metrics.aggregation import task_mean
from tau2.voice.audio_activity import (
    USER_EVENT_VERSION,
    merge_intervals,
    speech_evidence,
)

VERSION = "tau-voice-interaction/4"
EVENT_VERSION = USER_EVENT_VERSION
METRIC_FIELDS = (
    "response_latency_mean",
    "response_latency_p90",
    "response_rate",
    "yield_latency_mean",
    "yield_rate",
    "agent_interruption_event_rate",
    "agent_interrupted_turn_rate",
    "speech_overlap_duration",
    "on_time_rate",
    "selectivity_backchannel",
    "selectivity_vocal_tic",
    "selectivity_non_directed",
)


def weighted_percentile(samples, p=0.9):
    """Return the inverse weighted empirical CDF, including zero delays."""
    ordered = sorted(samples)
    if not ordered:
        return None
    threshold = p * fsum(w for _, w in ordered)
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold - 1e-12:
            return value
    return ordered[-1][0]


def group_speech(intervals):
    """Group short pauses without changing the underlying voiced intervals."""
    result = []
    for a, b in merge_intervals(intervals):
        if result and a - result[-1][1] < CONFIG["merge_gap_sec"] - 1e-9:
            result[-1][1] = b
        else:
            result.append([a, b])
    return result


def _active(intervals, at):
    return any(a <= at < b for a, b in intervals)


def _stop(start, agent, cutoff, deadline=None):
    """Find a one-second silence; timestamp its start, not confirmation."""
    cursor = start
    for a, b in agent:
        if b <= start:
            continue
        if a - cursor >= CONFIG["stop_silence_sec"] - 1e-9:
            if deadline is None or cursor <= deadline:
                return dict(
                    status="stopped",
                    stopped=cursor,
                    confirmed=cursor + CONFIG["stop_silence_sec"],
                )
            return dict(status="not_stopped")
        cursor = max(cursor, b)
    if deadline is not None and cursor > deadline:
        return dict(status="not_stopped")
    if cutoff - cursor >= CONFIG["stop_silence_sec"] - 1e-9:
        return dict(
            status="stopped",
            stopped=cursor,
            confirmed=cursor + CONFIG["stop_silence_sec"],
        )
    if cutoff > cursor:
        return dict(status="insufficient_observation", silence_started=cursor)
    return dict(status="not_stopped")


def _ratio(n, d):
    return n / d if d else None


def evaluate(activity, calls=None, task_key=None):
    """Derive all twelve metrics once from the same classified events."""
    report = dict(
        version=VERSION,
        config=dict(CONFIG),
        namespace="voice_interaction",
        task_key=task_key,
        status="unavailable",
        metrics={name: {"value": None} for name in METRIC_FIELDS},
        evaluation_log={},
    )
    if not activity or activity.get("user_event_version") != EVENT_VERSION:
        report["reason"] = "classified_user_events_unavailable"
        return report
    cutoff = activity["duration"]
    agent = merge_intervals(activity["agent_intervals"])
    groups = group_speech(agent)
    events = sorted(activity.get("user_events", []), key=lambda r: r["intervals"][0][0])
    formal = [r for r in events if r["kind"] == "formal"]
    responses, yields, interruptions, overlaps = [], [], [], []
    for index, turn in enumerate(formal):
        start, end = turn["intervals"][0][0], turn["intervals"][-1][1]
        until = (
            formal[index + 1]["intervals"][0][0] if index + 1 < len(formal) else cutoff
        )
        # A response must begin after the user has finished.  Continuing
        # speech that started before ``end`` belongs to the previous Agent
        # group: it may be an interruption/overlap, but it is not a response
        # to this formal utterance.
        began = next((a for a, b in groups if end <= a < until), None)
        response = dict(
            user_id=turn["id"],
            start=start,
            end=end,
            until=until,
            responded=began is not None,
            response_start=began,
            latency=began - end if began is not None else None,
        )
        response["on_time"] = None
        if began is not None:
            limit = CONFIG["on_time_sec"]
            response.update(on_time_limit=limit, on_time=began - end < limit)
        responses.append(response)
        if any(a < start < b for a, b in groups):
            stopped = _stop(start, agent, cutoff)
            yields.append(
                dict(
                    user_id=turn["id"],
                    start=start,
                    **stopped,
                    latency=stopped["stopped"] - start
                    if stopped["status"] == "stopped"
                    else None,
                )
            )
        for group_id, (a, b) in enumerate(groups):
            if start < a < end:
                interruptions.append(
                    dict(user_id=turn["id"], group_id=group_id, start=a)
                )
        overlaps.append(
            dict(
                user_id=turn["id"],
                seconds=fsum(
                    max(0.0, min(b, d) - max(a, c))
                    for a, b in turn["intervals"]
                    for c, d in agent
                ),
            )
        )
    selectivity = []
    for event in events:
        kind = event["kind"]
        if kind not in ("backchannel", "vocal_tic", "non_directed"):
            continue
        start, end = event["intervals"][0][0], event["intervals"][-1][1]
        if _active(groups, start):
            stopped = _stop(
                start, agent, cutoff, start + CONFIG[kind + "_stop_window_sec"]
            )
            selectivity.append(
                dict(
                    user_id=event["id"],
                    kind=kind,
                    **stopped,
                    error=stopped["status"] == "stopped"
                    if stopped["status"] != "insufficient_observation"
                    else None,
                )
            )
        elif kind != "backchannel":
            until = end + CONFIG[kind + "_response_window_sec"]
            reacted = any(start <= a <= until for a, b in groups)
            selectivity.append(
                dict(user_id=event["id"], kind=kind, status="observed", error=reacted)
            )
    latencies = [r["latency"] for r in responses if r["responded"]]
    known_yields = [r for r in yields if r["status"] != "insufficient_observation"]
    stopped_yields = [r for r in yields if r["status"] == "stopped"]
    timed = [r for r in responses if r["on_time"] is not None]
    values = dict(
        response_rate=_ratio(len(latencies), len(formal)),
        response_latency_mean=mean(latencies) if latencies else None,
        response_latency_p90=weighted_percentile([(v, 1.0) for v in latencies]),
        yield_rate=_ratio(len(stopped_yields), len(known_yields)),
        yield_latency_mean=mean(r["latency"] for r in stopped_yields)
        if stopped_yields
        else None,
        agent_interruption_event_rate=_ratio(len(interruptions), len(formal)),
        agent_interrupted_turn_rate=_ratio(
            len({r["user_id"] for r in interruptions}), len(formal)
        ),
        speech_overlap_duration=mean(r["seconds"] for r in overlaps)
        if overlaps
        else None,
        on_time_rate=_ratio(sum(r["on_time"] for r in timed), len(timed)),
    )
    counts = dict(
        formal_turns=len(formal),
        responses=len(latencies),
        yields=len(stopped_yields),
        yield_total=len(known_yields),
        yield_observation_insufficient=len(yields) - len(known_yields),
        agent_interruptions=len(interruptions),
        interrupted_turns=len({r["user_id"] for r in interruptions}),
    )
    for kind in ("backchannel", "vocal_tic", "non_directed"):
        observed = [
            r for r in selectivity if r["kind"] == kind and r["error"] is not None
        ]
        errors = sum(r["error"] for r in observed)
        values["selectivity_" + kind] = _ratio(len(observed) - errors, len(observed))
        counts[kind + "_total"], counts[kind + "_errors"] = len(observed), errors
    report.update(
        status="measured",
        metrics={k: {"value": v} for k, v in values.items()},
        evaluation_log=dict(
            responses=responses,
            yields=yields,
            interruptions=interruptions,
            overlaps=overlaps,
            selectivity=selectivity,
            counts=counts,
            user_events=events,
            agent_groups=groups,
            cutoff=cutoff,
        ),
    )
    return report


def from_ticks(ticks, calls=None, task_key=None):
    """Measure new media records without guessing missing historical categories."""
    return evaluate(speech_evidence(ticks), calls, task_key)


def aggregate(reports):
    """Average trials then tasks; P90 uses equal task/trial sample weights."""
    valid = [
        (i, r)
        for i, r in enumerate(reports)
        if r
        and r.get("version") == VERSION
        and r.get("status") == "measured"
        and not r.get("diagnostic_only")
    ]

    def key(i, r):
        value = r.get("task_key")
        return tuple(value) if value is not None else ("trial", i)

    values = {
        name: task_mean((key(i, r), r["metrics"][name]["value"]) for i, r in valid)
        for name in METRIC_FIELDS
        if name != "response_latency_p90"
    }
    tasks = defaultdict(list)
    for i, r in valid:
        samples = [
            v["latency"] for v in r["evaluation_log"]["responses"] if v["responded"]
        ]
        if samples:
            tasks[key(i, r)].append(samples)
    weighted = [
        (value, 1 / (len(tasks) * len(trials) * len(samples)))
        for trials in tasks.values()
        for samples in trials
        for value in samples
    ]
    values["response_latency_p90"] = weighted_percentile(weighted)
    return dict(
        version=VERSION,
        config=dict(CONFIG),
        namespace="voice_interaction",
        metrics={name: {"value": values[name]} for name in METRIC_FIELDS},
        evaluation_log=dict(
            total_trials=len(reports),
            measured_trials=len(valid),
            applicable_tasks={
                name: len(
                    {
                        key(i, r)
                        for i, r in valid
                        if r["metrics"][name]["value"] is not None
                    }
                )
                for name in METRIC_FIELDS
            },
            historical_or_unavailable_trials=len(reports) - len(valid),
        ),
    )


def experiment_reports(results):
    """Build trial reports with dataset/language/task identities for new runs."""
    if not any(sim.ticks for sim in results.simulations):
        return []
    info = results.info.model_dump(mode="json")
    reports = []
    for sim in results.simulations:
        if not sim.ticks:
            continue
        context = getattr(sim, "language_context", None)
        context = context.model_dump(mode="json") if context is not None else {}
        context = context or info.get("language_tasks", {}).get(str(sim.task_id), {})
        key = (
            info.get("environment_info", {}).get("domain_name"),
            context.get("language") or info.get("language"),
            context.get("task_selection", {}).get("task_set_name")
            or info.get("task_set_name"),
            str(sim.task_id),
        )
        reports.append(from_ticks(sim.ticks, task_key=key))
    return reports


def experiment_panel(results):
    """Return a unified panel, or None when every trajectory is historical."""
    reports = experiment_reports(results)
    if not any(r["status"] == "measured" for r in reports):
        return None
    report = aggregate(reports)
    counts = defaultdict(int)
    for trial in reports:
        for name, count in trial.get("evaluation_log", {}).get("counts", {}).items():
            counts[name] += count
    return dict(
        version=VERSION,
        **{k: v["value"] for k, v in report["metrics"].items()},
        evaluation_log=dict(
            report["evaluation_log"], counts=dict(counts), trial_reports=reports
        ),
    )
