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
