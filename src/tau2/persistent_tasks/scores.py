"""One completion value with reusable native-result and reply diagnostics."""


def completion_score(result):
    """Read a saved total without replacing an explicit zero or unavailable value."""
    for name in ("completion_score", "reward", "native_reward"):
        if name in (result or {}):
            return result[name]
    return None


def consolidate_score(result):
    """Project old/new score records without re-running any grading decisions."""
    result = dict(result)
    log = dict(result.get("evaluation_log") or {})
    for name in ("operation_reward", "coverage"):
        if name in result:
            log.setdefault(name, result.pop(name))
    native = dict(log.get("native_result") or {})
    reply = dict(log.get("reply") or {})
    for name in ("native_result_reward", "state_result_reward"):
        if name in result and "passed" not in native:
            value = result[name]
            native["passed"] = bool(value) if value is not None else None
    if "reply_reward" in result and "passed" not in reply:
        value = result["reply_reward"]
        reply["passed"] = bool(value) if value is not None else None
    if "checks" in result:
        native["checks"] = result.pop("checks")
    if "reply_checks" in result:
        reply["checks"] = result.pop("reply_checks")
    if native:
        log["native_result"] = native
    if reply:
        log["reply"] = reply
    total = completion_score(result)
    for name in (
        "reward",
        "native_reward",
        "native_result_reward",
        "state_result_reward",
        "reply_reward",
    ):
        result.pop(name, None)
    return {**result, "completion_score": total, "evaluation_log": log}
