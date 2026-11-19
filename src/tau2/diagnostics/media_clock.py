"""Mapping between scenario timestamps and the delivered media clock."""

from math import isfinite
from typing import Any

MEDIA_CLOCK_VERSION = "tau-media-clock/2"


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
    )


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "media_seconds": None,
        "source": None,
        "distance_seconds": None,
        "reason": reason,
    }


def _interpolate(
    target: float,
    start: float,
    end: float,
    media_start: float,
    media_end: float,
):
    if end <= start:
        return None
    return media_start + (target - start) / (end - start) * (media_end - media_start)


def map_scenario_time_to_media(
    value: float,
    timeline: list[dict[str, Any]],
    scenario_start_time: float | None,
    scenario_monotonic_origin: float | None,
) -> dict[str, Any]:
    """Map one scenario timestamp through tick interiors or an adjacent gap.

    A gap is a real interval between recorded tick receipts. Its media position
    is bounded by the preceding tick's end and the following tick's start; no
    host-clock extrapolation is used before or after the recorded timeline.
    """

    if (
        not _number(value)
        or not _number(scenario_start_time)
        or not _number(scenario_monotonic_origin)
    ):
        return _unavailable("missing_scenario_clock")
    if not timeline:
        return _unavailable("missing_media_timeline")
    target = value - scenario_start_time + scenario_monotonic_origin
    rows = []
    for row in timeline:
        mono_start, mono_end = row.get("monotonic_start"), row.get("monotonic_end")
        media_start, media_end = row.get("start"), row.get("end")
        if (
            not all(
                _number(item) for item in (mono_start, mono_end, media_start, media_end)
            )
            or mono_end <= mono_start
            or media_end < media_start
        ):
            return _unavailable("invalid_media_timeline")
        rows.append((mono_start, mono_end, media_start, media_end))
    rows.sort(key=lambda item: (item[0], item[1]))
    if target < rows[0][0] or target > rows[-1][1]:
        return _unavailable("outside_media_timeline")
    for mono_start, mono_end, media_start, media_end in rows:
        if mono_start <= target <= mono_end:
            media = _interpolate(target, mono_start, mono_end, media_start, media_end)
            if media is None:
                return _unavailable("invalid_media_timeline")
            return {
                "status": "mapped",
                "media_seconds": media,
                "source": "tick",
                "distance_seconds": 0.0,
                "reason": None,
            }
    for previous, following in zip(rows, rows[1:]):
        prev_start, prev_end, prev_media_start, prev_media_end = previous
        next_start, next_end, next_media_start, next_media_end = following
        if prev_end < target < next_start:
            media = _interpolate(
                target,
                prev_end,
                next_start,
                prev_media_end,
                next_media_start,
            )
            if media is None:
                return _unavailable("invalid_media_timeline")
            return {
                "status": "mapped",
                "media_seconds": media,
                "source": "gap_boundary",
                "distance_seconds": min(target - prev_end, next_start - target),
                "reason": None,
            }
    # The only remaining case is an overlap or an exact boundary that was not
    # covered by the inclusive interior check. Treat it as a known clock issue.
    return _unavailable("nonmonotonic_media_timeline")
