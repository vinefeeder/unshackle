"""Offline tests for Pluto's web playout metadata adapter."""

from copy import deepcopy
from lxml import etree
import pytest

from unshackle.services.PLUTO.playout import plan_playout, session_jwt_from_html


def playout():
    return {
        "streamInfo": {"streamType": "episode", "streamID": "ep-1"},
        "clips": [
            {"type": "clip", "episodeID": "ep-1", "clipStartInEpisodeMs": 0,
             "startTime": "2026-11-21T10:00:00.000Z", "endTime": "2026-11-21T10:00:01.000Z"},
            {"type": "creative", "startTime": "2026-11-21T10:00:01.000Z",
             "endTime": "2026-11-21T10:00:03.000Z"},
            {"type": "clip", "episodeID": "ep-1", "clipStartInEpisodeMs": 1000,
             "startTime": "2026-11-21T10:00:03.000Z", "endTime": "2026-11-21T10:00:06.000Z"},
        ],
    }


def manifest():
    return etree.fromstring(
        b'<MPD><Period id="one" start="PT0S" duration="PT1S"/>'
        b'<Period id="ad" start="PT1S" duration="PT2S"/>'
        b'<Period id="two" start="PT3S" duration="PT3S"/></MPD>'
    )


def test_selects_programme_and_preserves_offsets():
    mpd = manifest()
    plan = plan_playout(mpd, playout())
    assert [part.index for part in plan.kept] == [0, 2]
    assert [part.programme_start_ms for part in plan.kept] == [0, 1000]
    assert [plan.filter(mpd)(part) for part in mpd] == [False, True, False]


def test_allows_nonzero_first_content_offset_with_continuous_following_clips():
    altered = deepcopy(playout())
    altered["clips"][0]["clipStartInEpisodeMs"] = 10_000
    altered["clips"][2]["clipStartInEpisodeMs"] = 11_000
    plan = plan_playout(manifest(), altered)
    assert [period.index for period in plan.kept] == [0, 2]
    assert [period.programme_start_ms for period in plan.kept] == [0, 1000]


def test_rejects_discontinuous_offset_after_nonzero_first_content_offset():
    altered = deepcopy(playout())
    altered["clips"][0]["clipStartInEpisodeMs"] = 10_000
    altered["clips"][2]["clipStartInEpisodeMs"] = 12_000
    with pytest.raises(ValueError, match="expected 11000 ms, got 12000 ms"):
        plan_playout(manifest(), altered)


def test_missing_source_offsets_still_selects_programme_periods():
    altered = deepcopy(playout())
    for entry in altered["clips"]:
        if entry["type"] == "clip":
            entry.pop("clipStartInEpisodeMs", None)
    plan = plan_playout(manifest(), altered)
    assert [period.index for period in plan.kept] == [0, 2]


def test_missing_first_source_offset_does_not_break_later_anchor():
    altered = deepcopy(playout())
    altered["clips"][0].pop("clipStartInEpisodeMs")
    altered["clips"][2]["clipStartInEpisodeMs"] = 100_000
    plan = plan_playout(manifest(), altered)
    assert [period.index for period in plan.kept] == [0, 2]


def test_missing_later_source_offset_does_not_break_first_anchor():
    altered = deepcopy(playout())
    altered["clips"][2].pop("clipStartInEpisodeMs")
    plan = plan_playout(manifest(), altered)
    assert [period.index for period in plan.kept] == [0, 2]


def test_invalid_present_source_offset_is_rejected():
    altered = deepcopy(playout())
    altered["clips"][0]["clipStartInEpisodeMs"] = "not-a-number"
    with pytest.raises(ValueError, match="Invalid programme offset"):
        plan_playout(manifest(), altered)


def test_rejects_stale_ad_schedule():
    altered = deepcopy(playout())
    altered["clips"][1]["endTime"] = "2026-11-21T10:00:04.000Z"
    with pytest.raises(ValueError, match="gap or overlap"):
        plan_playout(manifest(), altered)


def test_rejects_wrong_episode():
    altered = deepcopy(playout())
    altered["clips"][2]["episodeID"] = "different"
    with pytest.raises(ValueError, match="episode ID"):
        plan_playout(manifest(), altered)


def test_rejects_changed_mpd():
    mpd = manifest()
    mpd[1].set("duration", "PT2.500S")
    with pytest.raises(ValueError, match="differ"):
        plan_playout(mpd, playout())


def test_rejects_unknown_clip_type():
    altered = deepcopy(playout())
    altered["clips"][1]["type"] = "unknown"
    with pytest.raises(ValueError, match="Unrecognised"):
        plan_playout(manifest(), altered)


def test_extract_bootstrap_jwt():
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"sessionJwt":"aaa.bbb.ccc"}}'
        '</script></html>'
    )
    assert session_jwt_from_html(html) == "aaa.bbb.ccc"


def test_bootstrap_missing_token_is_error():
    with pytest.raises(ValueError, match="sessionJwt"):
        session_jwt_from_html(
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{}}</script>'
        )


def test_bootstrap_missing_script_is_error():
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        session_jwt_from_html("<html></html>")
