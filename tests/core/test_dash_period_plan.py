"""Unit tests for strict multi-period DASH planning."""

from __future__ import annotations

import pytest
from lxml import etree

from unshackle.core.manifests.period_plan import PeriodPlan, TimelineInterval, duration_ms


def manifest():
    return etree.fromstring(
        b'<MPD>'
        b'<Period id="programme-a" start="PT0S" duration="PT1S"/>'
        b'<Period id="advert" start="PT1S" duration="PT2S"/>'
        b'<Period id="programme-b" start="PT3S" duration="PT3S"/>'
        b'</MPD>'
    )


def test_duration_ms():
    assert duration_ms("PT1M2.345S") == 62345
    assert duration_ms("PT0.001S") == 1


def test_period_selection_and_offsets():
    mpd = manifest()
    plan = PeriodPlan.build(mpd, [
        TimelineInterval(0, 1000, True),
        TimelineInterval(1000, 2000, False),
        TimelineInterval(3000, 3000, True),
    ])
    assert [period.index for period in plan.kept] == [0, 2]
    assert [period.programme_start_ms for period in plan.kept] == [0, 1000]
    assert [plan.filter(mpd)(period) for period in mpd] == [False, True, False]


def test_reject_mismatched_session():
    with pytest.raises(ValueError, match="differ"):
        PeriodPlan.build(manifest(), [
            TimelineInterval(0, 1000, True),
            TimelineInterval(1000, 2500, False),
            TimelineInterval(3000, 3000, True),
        ])


def test_reject_incomplete_session():
    with pytest.raises(ValueError, match="intervals"):
        PeriodPlan.build(manifest(), [TimelineInterval(0, 1000, True)])


def test_reject_all_filtered():
    with pytest.raises(ValueError, match="every period"):
        PeriodPlan.build(manifest(), [
            TimelineInterval(0, 1000, False),
            TimelineInterval(1000, 2000, False),
            TimelineInterval(3000, 3000, False),
        ])
