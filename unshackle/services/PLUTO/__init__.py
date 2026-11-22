from __future__ import annotations

import base64
import concurrent
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin

import click
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from envied.core.credential import Credential
from envied.core.manifests import DASH, HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Chapter, Chapters, Tracks


class PLUTO(Service):
    """
    \b
    Service code for Pluto TV on demand streaming service (https://pluto.tv/)

    \b
    Version: 1.0.5
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      Widevine:
        L3: 1080p, AAC2.0

    \b
    Tips:
        - Input examples:
           SERIES: https://pluto.tv/us/shows/frasier-cbs-tv/
           EPISODE: https://pluto.tv/us/shows/2180151/episode/6356cd01df7dfa0013468858/
           MOVIE: https://pluto.tv/us/movies/69276e8c8e21e6575a019128/
        - Use --lang LANG_RANGE option to request non-English tracks
        - Use --hls to request HLS instead of DASH:
           unshackle dl pluto URL --hls

    \b
    Notes:
        - Both DASH(widevine) and HLS(AES) are looked for in the API.
        - DASH is prioritized over HLS since the latter doesn't have 1080p. If DASH has audio/subtitle issues,
          you can try using HLS with the --hls flag.
        - Pluto use transport streams for HLS, meaning the video and audio are a part of the same stream
          As a result, only videos are listed as tracks. But the audio will be included as well.

    """

    ALIASES = ("plutotv",)
    TITLE_RE = (
        r"^"
        r"(?:https?://)?(?:www\.)?pluto\.tv"
        r"(?:/(?!(?:on-demand|movies|shows|series)\b)[a-z]{2,}(?:_[a-z]{2,})*)?"
        r"(?:/on-demand)?"
        r"/(?P<type>movies|shows|series)"
        r"/(?P<id>[a-z0-9-]+)"
        r"(?:/(?:season/\d+/)?episode/(?P<episode>[a-z0-9-]+))?"
        r"/?$"
    )

    @staticmethod
    @click.command(name="PLUTO", short_help="https://pluto.tv/", help=__doc__)
    @click.option("--hls", is_flag=True, help="Request HLS instead of DASH")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs) -> PLUTO:
        return PLUTO(ctx, **kwargs)

    def __init__(self, ctx, title: str, hls=False):
        super().__init__(ctx)
        self.title = title
        self.force_hls = hls

    def authenticate(
        self,
        cookies: CookieJar | None = None,
        credential: Credential | None = None,
    ) -> None:
        super().authenticate(cookies, credential)

        headers = {
            "cache-control": "no-cache",
            "user-agent": "Pluto TV/17.2.0 okhttp/5.5.0",
            "content-type": "application/json",
        }

        payload = {
            "deviceId": str(uuid.uuid4()),
            "deviceMake": "Nvidia",
            "deviceModel": "SHIELD Android TV",
            "deviceVersion": "mdarcy",
            "appVersion": "17.2.0",
            "ptvAppName": "androidtv",
            "drmCapabilities": "widevine:L1",
            "isClientDNT": False,
        }

        start = self._request("POST", "/apps-api/v3.1/androidtv/platform/start.json", headers=headers, json=payload)

        if not (token := start.get("session", {}).get("jwt")):
            raise ValueError("Failed to get session token")
        
        self.session_token = token
        self.region = start["session"].get("country", "")

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "term": self.title,
            "showCanVids": "false",
            "showBadges": "true",
            "termCount": "10",
        }
        results = self._request("GET", "/apps-api/v3.2/androidtv/content/contentsearch/search.json", params=params)["terms"]

        for result in results:
            content_id = result.get("contentId")
            kind = result.get("term_type")
            show_path = result.get("path")

            if kind == "show":
                path = f"/{show_path}"
            elif kind == "movie":
                path = f"/movies/{content_id}"
            else:
                continue

            yield SearchResult(
                id_=f"https://pluto.tv{path}",
                title=result.get("title"),
                description=result.get("about"),
                label=kind,
                url=f"https://pluto.tv{path}",
            )

    def get_titles(self) -> Titles_T:
        parsed = self.parse_title(self.title)
        if not parsed:
            self.log.error(f"Failed to parse title: {self.title}")
            return
        
        kind = parsed.get("type")
        slug = parsed.get("slug")
        episode_id = parsed.get("episode")

        if kind == "show" and episode_id:
            episode = self._episode(episode_id)
            return Series(episode)

        elif kind == "show":
            episodes = self._show(slug)
            return Series(episodes)

        elif kind == "movies":
            movie = self._movie(slug)
            return Movies(movie)

    def get_tracks(self, title: Title_T) -> Tracks:
        """
        The old endpoint for ad-free manifests still work, so we use that for now.
        The new format is included below for future reference.
        """

        # headers = {
        #     "user-agent": "Pluto TV/17.2.0 okhttp/5.5.0",
        # }

        # params = {
        #     "appName": "androidtv",
        #     "appVersion": "17.2.0",
        #     "contentId": title.id,
        #     "contentType": "vod",
        #     "drm": "widevine",
        #     "drmLevel": "L1",
        #     "streamType": "stitcher",
        #     "locale": "en-us",
        # }
        # data = self._request("GET", "/apps-api/v4.0/androidtv/streaming-url.json", headers=headers, params=params)

        # path = next((source for source in data["stitcherPaths"] if source.get("type") == "mpd"), None)
        # source = path.get("path")
        # manifest = f"https://ipv4.pluto.tv/apps-api/playout{source}"
        # params = {
        #     "includeExtendedEvents": "true",
        # }
        # headers = {
        #     "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; SHIELD Android TV Build/RQ1A.210105.003)",
        #     "Host": "ipv4.pluto.tv",
        #     "Connection": "Keep-Alive",
        #     "Authorization": f"Bearer {self.session_token}",
        #     "X-Api-Access-Token": "ABDY8izoLeZAfB7dNR/hf0nHZSkLaiR4l7UnkVIA51creyHgeW63NMfl90VOLO0iFRU=",
        # }
        # r = self.session.get(manifest, headers=headers, params=params)
        # r.raise_for_status()
        # tracks = DASH.from_text(r.text, manifest).to_tracks(language=title.language)

        response = self.session.get(self.config["endpoints"]["episodes"].format(episode_id=title.id))
        if not response.ok:
            raise ConnectionError(response.text)

        episode = response.json()

        sources = next((item.get("sources") for item in episode if not self.bumpers(item.get("name", ""))), None)

        if not sources:
            raise ValueError("Unable to find manifest for this title")

        hls = next((x.get("file") for x in sources if x.get("type").lower() == "hls"), None)
        dash = next((x.get("file") for x in sources if x.get("type").lower() == "dash"), None)

        if dash and not self.force_hls:
            manifest = dash.replace("https://siloh.pluto.tv", "http://silo-hybrik.pluto.tv.s3.amazonaws.com")
            tracks = DASH.from_url(manifest, self.session).to_tracks(language=title.language)

            for track in tracks.audio:
                track.language = title.language
                role = track.data["dash"]["adaptation_set"].find("Role")
                if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                    track.descriptive = True

        else:
            m3u8_url = hls.replace("https://siloh.pluto.tv", "http://silo-hybrik.pluto.tv.s3.amazonaws.com")
            manifest = self.clean_manifest(self.session.get(m3u8_url).text)
            tracks = HLS.from_text(manifest, m3u8_url).to_tracks(language=title.language)

            # Remove separate AD audio tracks
            for track in tracks.audio:
                tracks.audio.remove(track)

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        if not title.data.get("playbackEvents"):
            return Chapters()
        
        events = {k: v for k, v in title.data.get("playbackEvents").items() if v is not None}
        if not events:
            return Chapters()
        
        chapters = []
        for event in events:  # noqa: PLC0206
            if event.lower() == "previewstarttimems":
                chapters.append(Chapter(name="Preview", timestamp=events[event]))
            elif event.lower() == "previewendtimems":
                chapters.append(Chapter(timestamp=events[event]))
            elif event.lower() == "opencreditstarttime":
                chapters.append(Chapter(name="Opening Credits", timestamp=events[event]))
            elif event.lower() == "opencreditendtimems":
                chapters.append(Chapter(timestamp=events[event]))
            elif event.lower() == "endcreditchaptertimems":
                chapters.append(Chapter(name="End Credits", timestamp=events[event]))
        
        chapters = sorted(chapters, key=lambda x: x.timestamp)
        return Chapters(chapters)

    def get_widevine_service_certificate(self, challenge: bytes, title: Title_T, **_: Any) -> bytes | str | None:
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; SHIELD Android TV Build/RQ1A.210105.003)",
            "Authorization": f"Bearer {self.session_token}",
            "X-Api-Access-Token": "ABDY8izoLeZAfB7dNR/hf0nHZSkLaiR4l7UnkVIA51creyHgeW63NMfl90VOLO0iFRU=",
        }

        r = self.session.post(url=self.config["endpoints"]["wv"], headers=headers, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        return r.content

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes | str | None:
        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; SHIELD Android TV Build/RQ1A.210105.003)",
            "Authorization": f"Bearer {self.session_token}",
            "X-Api-Access-Token": "ABDY8izoLeZAfB7dNR/hf0nHZSkLaiR4l7UnkVIA51creyHgeW63NMfl90VOLO0iFRU=",
        }

        r = self.session.post(url=self.config["endpoints"]["wv"], headers=headers, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        return r.content

    def get_playready_license(self, *, challenge: str, **_: Any) -> bytes | str | None:
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; SHIELD Android TV Build/RQ1A.210105.003)",
            "Authorization": f"Bearer {self.session_token}",
            "X-Api-Access-Token": "ABDY8izoLeZAfB7dNR/hf0nHZSkLaiR4l7UnkVIA51creyHgeW63NMfl90VOLO0iFRU=",
        }

        r = self.session.post(url=self.config["endpoints"]["pr"], headers=headers, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.status_code, r.text)

        return r.content

    # service-specific functions

    def get_season_episodes(self, season: int, section: str) -> list:
        try:
            res = self._request(
                "GET",
                f"/apps-api/v2.0/androidtv/videos/section/{section}.json",
                params={"begin": "0", "rows": "999", "params": f"seasonNum={season}", "seasonNum": season},
            )
            return res.get("sectionItems", {}).get("itemList", [])
        except Exception as e:
            self.log.error(f"Error fetching data for season {season}: {e}")
            return []

    def _show(self, title: str) -> Episode:
        data = self._request("GET", f"/apps-api/v3.0/androidtv/shows/slug/{title}.json")

        links = next((x.get("links") for x in data["showMenu"] if x.get("device_app_id") == "all_platforms"), None)
        config = next((x.get("videoConfigUniqueName") for x in links if "Episodes" in x.get("title", "")), "SHOW_LANDING_EPISODES")
        show = next((x for x in data["show"]["results"] if x.get("type") == "show"), None)
        seasons = [x["seasonNum"] for x in data["available_video_seasons"]["itemList"] if x.get("seasonNum")]
        language = show.get("audienceLanguage", "en-US")

        show_data = self._request(
            "GET",
            "/apps-api/v2.0/androidtv/shows/{}/videos/config/{}.json".format(show.get("show_id"), config),
            params={"platformType": "apps", "rows": "1", "begin": "0"},
        )

        section = next(
            (x["sectionId"] for x in show_data["videoSectionMetadata"] if x["title"] == "Full Episodes"), None
        )

        episodes = []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_season = {executor.submit(self.get_season_episodes, season, section): season for season in seasons}
            for future in concurrent.futures.as_completed(future_to_season):
                episodes.extend(future.result())

        return [
            Episode(
                id_=episode.get("contentId"),
                title=episode.get("seriesTitle"),
                season=episode.get("seasonNum", 0),
                number=episode.get("episodeNum") or episode.get("positionNum"),
                name=episode["label"],
                language=language,
                service=self.__class__,
                # year=episode.get("originalReleaseYear"),
                data=episode,
            )
            for episode in episodes
            if episode["fullEpisode"]
        ]

    def _episode(self, title: str) -> Episode:
        data = self._request("GET", f"/apps-api/v2.0/androidtv/video/cid/{title}.json")

        return [
            Episode(
                id_=episode.get("contentId"),
                title=episode.get("seriesTitle"),
                season=episode.get("seasonNum", 0),
                number=episode.get("episodeNum") or episode.get("positionNum"),
                name=episode.get("label"),
                language=episode.get("audienceLanguage", "en-US"),
                service=self.__class__,
                data=episode,
            )
            for episode in data["itemList"]
            if episode["fullEpisode"]
        ]

    def _movie(self, title: str) -> Movie:
        res = self._request(
            "GET", f"/apps-api/v3.0/androidtv/movies/{title}.json", params={"includeContentInfo": "true"}
        )
        title = res["movie"]["movieContent"]
        return [
            Movie(
                id_=title.get("contentId"),
                name=title.get("title"),
                year=title.get("originalReleaseYear"),
                language=res["movie"].get("audienceLanguage", "en-US"),
                service=self.__class__,
                data=title,
            )
        ]

    def _request(self, method: str, endpoint: str, **kwargs) -> Any[dict | str]:
        url = urljoin(self.config["endpoints"]["base_url"], endpoint)

        if not kwargs.get("params"):
            kwargs["params"] = {}
        kwargs.get("params").update({"at": self._auth_token()})

        response = self.session.request(method, url, **kwargs)

        if response.status_code != 200:
            raise ConnectionError(f"{response.status_code}: {response.text}")

        try:
            data = json.loads(response.content)
            if (success := data.get("success")) is not None and not success:
                self.log.error(data.get("message"))
                sys.exit(1)
            
            return data

        except json.JSONDecodeError:
            return response.text

    def _auth_token(self) -> str:
        key = bytes.fromhex(self.config["key_spec"])
        app_secret = self.config["app_secret"]

        timestamp_ms = int(time.time() * 1000)
        payload = f"{timestamp_ms}|{app_secret}"
        plaintext_bytes = payload.encode("utf-8")

        iv = os.urandom(16)
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        padded_data = pad(plaintext_bytes, AES.block_size, style="pkcs7")
        encrypted_payload = cipher.encrypt(padded_data)

        token_data = b"\x00\x10" + iv + encrypted_payload
        return base64.b64encode(token_data).decode("utf-8")

    def parse_title(self, url: str) -> dict | None:
        m = re.match(self.TITLE_RE, url)
        if not m:
            return None
        t = m.group("type")
        return {
            "type": "show" if t in ("shows", "series") else t,
            "slug": m.group("id"),
            "episode": m.group("episode"),
        }

    @staticmethod
    def clean_manifest(text: str) -> str:
        # Remove fairplay entries
        index = text.find('#PLUTO-DRM:ID="fairplay')
        if index == -1:
            return text
        else:
            end_of_previous_line = text.rfind("\n", 0, index)
            if end_of_previous_line == -1:
                return ""
            else:
                return text[:end_of_previous_line]

    @staticmethod
    def bumpers(text: str) -> bool:
        ads = (
            "Pluto_TV_OandO",
            "_ad",
            "creative",
            "Bumper",
            "Promo",
            "WarningCard",
        )

        return any(ad in text for ad in ads)
        
