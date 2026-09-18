"""V8 associates interruption evidence by response, never by clear arrival order."""

from __future__ import annotations

import hashlib
import json

VERSION = "gaia2-qwen-audio/8"
POLICY = {
    "name": "response_bound_interruption",
    "version": 1,
    "primary": "same_response_provider_cancelled_turn_detected_and_client_cancel",
    "fallback": "unique_active_playback_at_user_clear_then_client_cancel",
    "changes_playback": False,
    "time_tolerance_sec": None,
}


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def classify_interruptions(metadata, events):
    """Pure function. Source row numbers refer to the original, unmodified log."""
    active, pending, reused = set(), set(), set()
    stopped, cleared = set(), set()
    clear_links, clear_records = {}, []
    by_id = {}
    provider_ids = {}
    conflicts = set()
    seen_rows = set()
    for index, row in enumerate(events, 1):
        row_hash = fingerprint(row)
        if row_hash in seen_rows:
            continue
        seen_rows.add(row_hash)
        kind = row.get("type")
        response_id = row.get("responseId")
        direction = row.get("direction")
        provider = (
            row.get("payload", {})
            if kind == "evaluation.provider_event" and direction == "server"
            else {}
        )
        if not isinstance(provider, dict):
            provider = {}
        response = provider.get("response", {})
        if not isinstance(response, dict):
            response = {}
        if provider.get("type") in {"response.created", "response.done"}:
            response_id = response.get("id")
            event_id = provider.get("event_id")
            if event_id:
                signature = fingerprint(provider)
                if event_id in provider_ids and provider_ids[event_id][0] != signature:
                    conflicts.update({response_id, provider_ids[event_id][1]})
                provider_ids[event_id] = (signature, response_id)
        if response_id:
            by_id.setdefault(response_id, []).append((index, row))
        if provider.get("type") == "response.created" and response_id in stopped:
            reused.add(response_id)
        if kind == "playback.started" and direction == "client" and response_id:
            if response_id in stopped:
                reused.add(response_id)
            active.add(response_id)
        elif kind in {"playback.cancelled", "playback.ended"} and direction == "client":
            active.discard(response_id)
            stopped.add(response_id)
        if (
            provider.get("type") == "response.done"
            and response.get("status") == "cancelled"
            and response_id
            and response_id not in cleared
        ):
            pending.add(response_id)
        if (
            kind == "playback.clear"
            and direction == "server"
            and row.get("reason") == "user_interruption"
        ):
            # A pending old cancellation must not lend its delayed unscoped clear to a new reply.
            ambiguous = bool(pending and active - pending)
            targets = sorted(active) if len(active) == 1 and not ambiguous else []
            record = {
                "row": index,
                "sha256": fingerprint(row),
                "wall_time": row.get("wall_time"),
                "active_response_ids": sorted(active),
                "pending_cancel_response_ids": sorted(pending),
                "attributed_response_ids": targets,
                "ambiguity": "late_clear_with_new_response"
                if ambiguous
                else "multiple_active_responses"
                if len(active) > 1
                else None,
            }
            clear_records.append(record)
            for identity in targets:
                clear_links.setdefault(identity, []).append(index)
                cleared.add(identity)
            pending.clear()

    decisions = []
    identities = [m.get("response_id") for m in metadata]
    for meta in metadata:
        if meta.get("played_completely") is True and not meta.get("cancelled"):
            continue
        identity = meta.get("response_id")
        rows = by_id.get(identity, [])
        starts = [
            i
            for i, r in rows
            if r.get("type") == "playback.started" and r.get("direction") == "client"
        ]
        cancels = [
            i
            for i, r in rows
            if r.get("type") == "playback.cancelled" and r.get("direction") == "client"
        ]
        ended = [
            i
            for i, r in rows
            if r.get("type") == "playback.ended" and r.get("direction") == "client"
        ]
        terminals = [
            (i, r["payload"]["response"])
            for i, r in rows
            if r.get("type") == "evaluation.provider_event"
            and r.get("direction") == "server"
            and r.get("payload", {}).get("type") == "response.done"
        ]
        bindings = {
            (r.get("turnId"), r.get("turnGeneration"))
            for _, r in rows
            if r.get("type") == "response.started" and r.get("direction") == "server"
        }
        terminal_types = {
            (
                r.get("status"),
                (r.get("status_details") or {}).get("reason")
                if isinstance(r.get("status_details") or {}, dict)
                else "malformed_status_details",
            )
            for _, r in terminals
        }
        received, played = meta.get("received_samples"), meta.get("played_samples")
        reason, basis = "unattributed_cancellation", None
        if (
            not identity
            or identities.count(identity) != 1
            or identity in reused
            or len(bindings) > 1
        ):
            reason = "ambiguous_response_identity"
        elif (
            type(received) is not int
            or type(played) is not int
            or not 0 < played <= received
            or meta.get("cancelled") is not True
            or meta.get("played_completely") is not False
        ):
            reason = "invalid_playback_metadata"
        elif identity in conflicts or len(terminal_types) > 1 or ended:
            reason = "conflicting_terminal_status"
        elif not starts or not cancels or min(cancels) <= min(starts):
            reason = "missing_or_invalid_playback_confirmation"
        elif terminal_types == {("cancelled", "turn_detected")}:
            basis, reason = "provider_response", "user_interruption"
        elif terminal_types and terminal_types != {("completed", None)}:
            reason = "unsupported_provider_terminal"
        elif any(min(starts) < i < min(cancels) for i in clear_links.get(identity, [])):
            basis, reason = "unique_active_clear", "user_interruption"
        referenced = sorted({i for i, _ in rows} | set(clear_links.get(identity, [])))
        decisions.append(
            {
                "response_id": identity,
                "decision": "normal_interruption"
                if basis
                else "unverified_interruption",
                "basis": basis,
                "reason": reason,
                "events": [
                    {
                        "row": i,
                        "sha256": fingerprint(events[i - 1]),
                        "type": events[i - 1].get("type"),
                        "wall_time": events[i - 1].get("wall_time"),
                    }
                    for i in referenced
                ],
            }
        )
    return {
        "schema": "gaia2-interruption-evidence/1",
        "policy": POLICY,
        "gateway_sha256": fingerprint(events),
        "responses": decisions,
        "clears": clear_records,
    }


def build_interruption_evidence(root):
    metadata = [json.loads(p.read_text()) for p in sorted(root.glob("response-*.json"))]
    events = [json.loads(s) for s in (root / "gateway.jsonl").read_text().splitlines()]
    return classify_interruptions(metadata, events)


def verify_interruption_evidence(root):
    report = build_interruption_evidence(root)
    if json.loads((root / "interruption-evidence.json").read_text()) != report:
        raise ValueError("Interruption proof differs from original gateway events")
    return report
