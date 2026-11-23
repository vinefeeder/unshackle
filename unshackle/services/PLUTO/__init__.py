"""Pluto TV web playback service.

Playback is obtained from Pluto's web JWT and stitched DASH manifest. The
playout-session JSON classifies ad periods; core DASH handles track selection.
Title discovery follows the web APIs used by the earlier service, not Android.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlparse

import click

from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapters, Tracks
from unshackle.services.PLUTO.playout import plan_playout, session_jwt_from_html


class PLUTO(Service):
    """Pluto TV on-demand playback through its current web endpoints.

    A playback test requires one fresh web JWT and a matching MPD / playout
    response. The legacy advert-free clips API is intentionally not used.
    """

    ALIASES = ("plu", "plutotv")
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?pluto\.tv)?"
        r"(?:/[a-z]{2}(?:_[a-z]{2})?)?"
        r"(?:/on-demand)?"
        r"/(?P<type>movies|series|shows)"
        r"/(?P<id>[a-z0-9-]+)"
        r"(?:/(?:season/\d+/)?episode/(?P<episode>[a-z0-9-]+))?"
        r"(?:/details)?/?$"
    )

    @staticmethod
    @click.command(name="PLUTO", short_help="https://pluto.tv/", help=__doc__)
    @click.option("--web-mpd", type=str, help="Override stitched DASH MPD URL (diagnostic)")
    @click.option("--web-playout", type=str, help="Override matching session JSON URL (diagnostic)")
    @click.option("--hls", is_flag=True, help="Unsupported with this experimental DASH service")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs) -> PLUTO:
        return PLUTO(ctx, **kwargs)

    def __init__(self, ctx, title: str, hls=False, web_mpd=None, web_playout=None):
        super().__init__(ctx)
        if hls:
            raise click.UsageError("Pluto web mode currently supports DASH only")
        if bool(web_mpd) != bool(web_playout):
            raise click.UsageError("--web-mpd and --web-playout must be supplied together")
        self.title = title
        self.web_mpd = web_mpd
        self.web_playout = web_playout
        self.web_jwt: str | None = None
        # Retained from the earlier web service for catalogue/search requests.
        # Do not attach these query parameters to the stitched MPD URL.
        self.catalogue_params = {
            "appName": "web",
            "appVersion": "na",
            "clientID": str(uuid.uuid4()),
            "deviceDNT": 0,
            "deviceId": "unknown",
            "clientModelNumber": "na",
            "serverSideAds": "false",
            "deviceMake": "unknown",
            "deviceModel": "web",
            "deviceType": "web",
            "deviceVersion": "unknown",
            "drmCapabilities": "widevine:L3",
        }

    def _web_headers(self) -> dict[str, str]:
        if not self.web_jwt:
            raise ValueError("Pluto web authentication has not completed")
        return {
            "Authorization": f"Bearer {self.web_jwt}",
            "User-Agent": self.config["web_user_agent"],
            "Referer": "https://pluto.tv/",
            "Origin": "https://pluto.tv",
            "Accept": "*/*",
        }

    def authenticate(
        self, cookies: CookieJar | None = None, credential: Credential | None = None
    ) -> None:
        super().authenticate(cookies, credential)
        # Optional override is useful for comparing an existing browser session.
        token = os.getenv("PLUTO_WEB_BEARER_TOKEN")
        if not token:
            response = self.session.get(
                self.config["endpoints"]["web_bootstrap"],
                headers={
                    "User-Agent": self.config["web_user_agent"],
                    "Accept": "text/html",
                    "Referer": "https://pluto.tv/",
                },
                timeout=30,
            )
            response.raise_for_status()
            token = session_jwt_from_html(response.text)
        self.web_jwt = token
        self.session_token = token  # Used by the existing licence-call convention.

    def _get_json(self, url: str, *, auth: bool = False, params: dict | None = None):
        headers = self._web_headers() if auth else {
            "User-Agent": self.config["web_user_agent"], "Referer": "https://pluto.tv/"
        }
        if url.startswith(("https://service-vod.", "https://service-media-search.")):
            params = {**self.catalogue_params, **(params or {})}
        response = self.session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def search(self) -> Generator[SearchResult, None, None]:
        data = self._get_json(
            self.config["endpoints"]["search"],
            auth=True,
            params={"q": self.title, "limit": "100"},
        )
        for entry in data.get("data", []):
            if entry.get("type") not in ("movie", "series", "show"):
                continue
            kind = "movies" if entry["type"] == "movie" else "series"
            item_id = entry.get("id")
            if not item_id:
                continue
            yield SearchResult(
                id_=f"https://pluto.tv/gb_ie/on-demand/{kind}/{item_id}/details",
                title=entry.get("name"),
                description=entry.get("synopsis"),
                label=entry.get("type"),
                url=f"https://pluto.tv/gb_ie/on-demand/{kind}/{item_id}/details",
            )

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title, re.I)
        if match is None:
            raise ValueError(f"Unable to parse Pluto title URL: {self.title}")
        kind, item_id, episode_id = (
            match.group("type"), match.group("id"), match.group("episode")
        )
        if kind in ("series", "shows"):
            # The web catalogue API is inherited from the original Envied
            # service. For a direct episode, use a clearly-labelled fallback
            # if catalogue lookup is unavailable in the current region.
            url = self.config["endpoints"]["series"].format(season_id=item_id)
            try:
                data = self._get_json(url)
                episodes = [
                    Episode(
                        id_=episode["_id"],
                        service=self.__class__,
                        title=data.get("name") or "Pluto TV",
                        season=int(episode.get("season") or 0),
                        number=int(episode.get("number") or 0),
                        name=episode.get("name"),
                        language="en",
                        data=episode,
                    )
                    for season in data.get("seasons", [])
                    for episode in season.get("episodes", [])
                    if not episode_id or episode.get("_id") == episode_id
                ]
            except Exception:
                if not episode_id:
                    raise
                self.log.warning("Web series catalogue unavailable; using direct episode ID")
                episodes = []
            if not episodes and episode_id:
                episodes = [
                    Episode(
                        id_=episode_id, service=self.__class__,
                        title="Pluto TV", season=0, number=0,
                        name=episode_id, language="en",
                    )
                ]
            if not episodes:
                raise ValueError("No episodes returned for this Pluto series")
            return Series(episodes)

        url = self.config["endpoints"]["movie"].format(video_id=item_id)
        movies = self._get_json(url, auth=True)
        return Movies([
            Movie(
                id_=item["_id"], service=self.__class__,
                name=item.get("name") or "Pluto TV",
                language="en", year=self._year(item), data=item,
            )
            for item in movies
        ])

    def get_tracks(self, title: Title_T) -> Tracks:
        if not isinstance(title, Episode):
            raise NotImplementedError(
                "Web stitched DASH movie URL is not yet verified; testing episodes first"
            )
        episode_id = str(title.id)
        manifest_url = self.web_mpd or self.config["endpoints"]["web_mpd"].format(
            episode_id=episode_id
        )
        session_url = self.web_playout or self.config["endpoints"]["web_playout"]
        # Avoid forwarding a bearer JWT to an arbitrary hostname from a CLI override.
        allowed = {"ipv4.pluto.tv"}
        if any(urlparse(url).hostname not in allowed for url in (manifest_url, session_url)):
            raise ValueError("Web playback endpoints must use ipv4.pluto.tv")

        # Fetch the MPD first, establishing this episode's stitched playout,
        # and only then retrieve the session's ad schedule.
        response = self.session.get(manifest_url, headers=self._web_headers(), timeout=30)
        response.raise_for_status()
        dash = DASH.from_text(response.text, response.url)
        session_params = None if self.web_playout else {"clientTime": int(time.time())}
        playout = self._get_json(session_url, auth=True, params=session_params)
        plan = plan_playout(dash.manifest, playout)
        if playout["streamInfo"]["streamID"] != episode_id:
            raise ValueError("Pluto session JSON belongs to a different episode")

        tracks = dash.to_tracks(
            language=title.language, period_filter=plan.filter(dash.manifest)
        )
        for track in (*tracks.videos, *tracks.audio):
            track.data["dash"]["period_aware"] = True
        self.log.info(
            "Validated Pluto playout: retaining %d of %d periods",
            len(plan.kept), plan.count,
        )
        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> None:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        headers = self._web_headers()
        headers["Content-Type"] = "application/octet-stream"
        response = self.session.post(
            self.config["endpoints"]["wv"], headers=headers,
            data=challenge, timeout=30,
        )
        response.raise_for_status()
        return response.content

    @staticmethod
    def _year(item: dict) -> int | None:
        for value in (item.get("name", ""), item.get("slug", "")):
            match = re.search(r"\b(19\d{2}|20\d{2})\b", value or "")
            if match:
                return int(match.group(1))
        return None
