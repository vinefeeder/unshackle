"""Pluto playout-to-DASH adapter.

Only the Pluto service interprets these API field names. The core downloader
receives a generic PeriodPlan, validated against the matching MPD.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from datetime import datetime, timezone
from typing import Any
from xml.etree.ElementTree import Element

from .period_plan import PeriodPlan, TimelineInterval


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
    advertising-inclusive timeline; clipStartInEpisodeMs, when supplied, describes source content time.
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
    first_programme_offset_ms: int | None = None
    first_programme_elapsed_ms = 0
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
            # Pluto does not provide this optional source offset for every VOD
            # title. Match all periods against their wall-clock playout times
            # regardless, and check source-offset continuity when it is present.
            if episode_offset is not None:
                if type(episode_offset) is not int or episode_offset < 0:
                    raise ValueError(f"Invalid programme offset at entry {index}")
                if first_programme_offset_ms is None:
                    first_programme_offset_ms = episode_offset
                    first_programme_elapsed_ms = programme_ms
                expected_offset = first_programme_offset_ms + programme_ms - first_programme_elapsed_ms
                if abs(episode_offset - expected_offset) > tolerance_ms:
                    raise ValueError(
                        f"Programme offset mismatch at entry {index}: "
                        f"expected {expected_offset} ms, got {episode_offset} ms"
                    )
            programme_ms += end - start
            if item.get("episodeID") != stream["streamID"]:
                raise ValueError(f"Pluto episode ID mismatch at entry {index}")
        intervals.append(TimelineInterval(start - first_start, end - start, keep))
        next_start = end

    return PeriodPlan.build(manifest, intervals, tolerance_ms=tolerance_ms)


class _NextDataScript(HTMLParser):
    """Read only the JSON script tagged __NEXT_DATA__, without executing JS."""

    def __init__(self):
        super().__init__()
        self.active = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.chunks.append(data)


def session_jwt_from_html(html: str) -> str:
    """Extract props.sessionJwt from the Pluto web bootstrap page."""
    parser = _NextDataScript()
    parser.feed(html)
    if not parser.chunks:
        raise ValueError("Pluto web page has no __NEXT_DATA__ script")
    data = json.loads("".join(parser.chunks))
    token = data.get("props", {}).get("sessionJwt")
    if not isinstance(token, str) or token.count(".") != 2:
        raise ValueError("Pluto bootstrap page has no usable props.sessionJwt")
    return token
