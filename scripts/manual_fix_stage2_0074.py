import copy
import hashlib
import json
from pathlib import Path

r = Path("data/simulations/gaia2-manual-takeover-v1")
ds = r / "dataset"


def sha(x):
    return hashlib.sha256(
        json.dumps(
            x,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def ref(e, p):
    return {"source": "result", "stage": 1, "event_id": e, "pointer": p}


configs = {
    "0429": (
        "series_s1_readback",
        "Messages__read_conversation",
        {"conversation_id": "{{series_s1_create}}", "limit": 10},
        "/messages/0/content",
    ),
    "0433": (
        "series_s1_verify",
        "Calendar__search_events",
        {"query": "Stockholm Brand Partnership Review"},
        "/events/0/title",
    ),
    "0473": (
        "series_s1_range",
        "Calendar__get_calendar_events_from_to",
        {
            "start_datetime": "2024-10-15 00:00:00",
            "end_datetime": "2024-11-01 23:59:59",
            "limit": 100,
        },
        "/events/0/title",
    ),
    "0536": (
        "series_s1_read_asa",
        "Chats__read_conversation",
        {"conversation_id": "86b6696d9634bf19ad16dcb2c4563ff", "limit": 3},
        "/metadata/conversation_title",
    ),
}
for pre, (rid, tool, args, ptr) in configs.items():
    p = next(
        p
        for p in (ds / "work").glob("*.json")
        if json.loads(p.read_text())["base_task_id"].startswith(pre)
    )
    d = json.loads(p.read_text())
    base = d.get("manual_pending_candidate", d["draft"])
    c = copy.deepcopy(base)
    st = c["stages"][0]
    ids = {e["id"] for e in st["events"]}
    if rid not in ids:
        st["events"].append(
            {
                "id": rid,
                "kind": "reference",
                "tool": tool,
                "arguments": args,
                "dependencies": ["series_s1_send"]
                if pre == "0429"
                else ["series_s1_user"],
                "channel": "tool",
            }
        )
    expected = next(
        (
            e["arguments"]["content"]
            for e in st["events"]
            if e["id"] == "series_s1_send"
        ),
        "Stockholm Brand Partnership Review"
        if pre == "0433"
        else "Inspection Day"
        if pre == "0473"
        else "Akira",
    )
    s2 = c["stages"][1]
    s2["evidence"] = [
        {
            "reference": ref(rid, ptr),
            "expected": expected,
            "public_tool": tool,
            "public_arguments": args,
            "result_pointer": ptr,
            "explanation": "Concrete public readback from stage one.",
        }
    ]
    for e in s2["events"]:
        e["dependencies"] = [
            x
            for x in e.get("dependencies", [])
            if x in {z["id"] for z in s2["events"]}
            or x in {"series_s2_user", "series_s1_create", "series_s1_send"}
        ] or ["series_s2_user"]
    plan = copy.deepcopy(d["question_plan"])
    plan["requests"][0]["request"] = st["request"]
    plan["requests"][1]["request"] = s2["request"]
    sub = {
        "task_id": d["base_task_id"],
        "base_candidate_sha256": sha(base),
        "candidate": c,
        "question_plan": plan,
        "reason": "Add unique public readback and bind stage-two evidence to concrete stage-one output.",
    }
    for f in ["submissions", "proposals"]:
        (r / f / f"{pre}-manual0074.json").write_text(
            json.dumps(sub, ensure_ascii=False, indent=2) + "\n"
        )
    print(pre)
