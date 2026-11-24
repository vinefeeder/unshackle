"""Service-local timing validator for Pluto clips and DASH periods.

Kept within the PLUTO service so stock Unshackle 7.0.0 core remains unchanged.
Other services may implement their own selection formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from xml.etree.ElementTree import Element


_DURATION = re.compile(r"^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")


def duration_ms(value: str) -> int:
    """Read an MPD period time in whole milliseconds, without float drift."""
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        raise ValueError(f"Unsupported DASH duration: {value!r}")
    hours, minutes, seconds = (Decimal(item or "0") for item in match.groups())
    millis = (hours * 3600 + minutes * 60 + seconds) * 1000
    if millis != millis.to_integral_value():
        raise ValueError(f"DASH duration exceeds millisecond precision: {value!r}")
    return int(millis)


@dataclass(frozen=True)
class TimelineInterval:
    """One interval in the advertising-inclusive, MPD-relative timeline."""

    start_ms: int
    duration_ms: int
    keep: bool


@dataclass(frozen=True)
class PlannedPeriod:
    index: int
    period_id: str | None
    start_ms: int
    duration_ms: int
    programme_start_ms: int


@dataclass(frozen=True)
class PeriodPlan:
    kept: tuple[PlannedPeriod, ...]
    discarded: tuple[int, ...]
    count: int

    @classmethod
    def build(
        cls,
        manifest: Element,
        intervals: Iterable[TimelineInterval],
        *,
        tolerance_ms: int = 50,
    ) -> PeriodPlan:
        """Require all MPD periods to match an entire ordered playout timeline.

        Failing closed avoids applying a different session's dynamic ads.
        """
        if tolerance_ms < 0:
            raise ValueError("tolerance_ms must be nonnegative")
        periods = [p for p in manifest if p.tag.rsplit("}", 1)[-1] == "Period"]
        entries = tuple(intervals)
        if not periods or len(entries) != len(periods):
            raise ValueError(f"Timeline has {len(entries)} intervals for {len(periods)} MPD periods")

        retained: list[PlannedPeriod] = []
        discarded: list[int] = []
        current_start = 0
        programme_start = 0

        for index, (period, entry) in enumerate(zip(periods, entries)):
            start = duration_ms(period.attrib["start"]) if "start" in period.attrib else current_start
            if "duration" not in period.attrib:
                raise ValueError(f"Period {index} lacks an explicit duration")
            duration = duration_ms(period.attrib["duration"])
            if duration <= 0 or entry.duration_ms <= 0:
                raise ValueError(f"Period {index} has an invalid duration")
            if abs(entry.start_ms - start) > tolerance_ms or abs(entry.duration_ms - duration) > tolerance_ms:
                raise ValueError(
                    f"Playout and MPD differ at period {index}: "
                    f"MPD=({start}, {duration}), playout=({entry.start_ms}, {entry.duration_ms})"
                )
            if entry.keep:
                retained.append(
                    PlannedPeriod(index, period.attrib.get("id"), start, duration, programme_start)
                )
                programme_start += duration
            else:
                discarded.append(index)
            current_start = start + duration

        if not retained:
            raise ValueError("Playout would remove every period")
        return cls(tuple(retained), tuple(discarded), len(periods))

    def filter(self, manifest: Element):
        """Return a predicate for DASH.to_tracks(period_filter=...).

        Use the same parsed manifest that was used to build the plan.
        """
        periods = [p for p in manifest if p.tag.rsplit("}", 1)[-1] == "Period"]
        if len(periods) != self.count:
            raise ValueError("Manifest changed since the period plan was constructed")
        excluded = {periods[i] for i in self.discarded}
        return lambda period: period in excluded
