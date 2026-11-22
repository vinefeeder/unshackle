"""Pluto playout-to-DASH adapter.

Only the Pluto service interprets these API field names. The core downloader
receives a generic PeriodPlan, validated against the matching MPD.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from xml.etree.ElementTree import Element

from unshackle.core.manifests.period_plan import PeriodPlan, TimelineInterval


def _timestamp_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("Pluto timestamps must have a timezone")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def plan_playout(manifest: Element, playout: dict[str, Any], *, tolerance_ms: int = 50) -> PeriodPlan:
    """Select Pluto programme periods, rejecting unrelated or incomplete playouts.

    Use data from one playback session for both inputs. Server times describe the
    advertising-inclusive timeline; clipStartInEpisodeMs describes content time.
    """
    clips = playout.get("clips")
    stream = playout.get("streamInfo")
    if not isinstance(clips, list) or not clips:
        raise ValueError("Pluto playout has no clips")
    if not isinstance(stream, dict) or not stream.get("streamID"):
        raise ValueError("Pluto playout has no stream identifier")

    first_start = _timestamp_ms(clips[0]["startTime"])
    next_start = first_start
    programme_ms = 0
    intervals: list[TimelineInterval] = []

    for index, item in enumerate(clips):
        if not isinstance(item, dict):
            raise ValueError(f"Invalid Pluto playout entry {index}")
        start = _timestamp_ms(item["startTime"])
        end = _timestamp_ms(item["endTime"])
        if end <= start or abs(start - next_start) > tolerance_ms:
            raise ValueError(f"Pluto playout has a gap or overlap at entry {index}")
        kind = item.get("type")
        keep = kind == "clip"
        if kind not in {"clip", "creative", "preInterstitial", "postInterstitial"}:
            raise ValueError(f"Unrecognised Pluto playout type {kind!r} at entry {index}")
        if keep:
            episode_offset = item.get("clipStartInEpisodeMs")
            if not isinstance(episode_offset, int) or abs(episode_offset - programme_ms) > tolerance_ms:
                raise ValueError(f"Programme offset mismatch at entry {index}")
            programme_ms += end - start
            if item.get("episodeID") != stream["streamID"]:
                raise ValueError(f"Pluto episode ID mismatch at entry {index}")
        intervals.append(TimelineInterval(start - first_start, end - start, keep))
        next_start = end

    return PeriodPlan.build(manifest, intervals, tolerance_ms=tolerance_ms)
