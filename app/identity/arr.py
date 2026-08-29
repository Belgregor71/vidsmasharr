"""Sonarr and Radarr as identity sources.

Secondary to Plex, but they cover a real gap: files the *arrs have imported but
Plex has not scanned yet, and libraries where Plex's own match is wrong. They
also carry the TVDB/TMDB ids, which are the identifiers the rest of the project
resolves titles against.

**Every call here is read-only, and this module stays that way.** It runs on
every `app identify`, so it should be incapable of changing anything. The guard
that writes custom formats to Sonarr and Radarr subclasses `ArrClient` in
`app/guard/arr_guard.py` and adds its verbs there, in the one module with a
reason for them. A test asserts this class has no `post`, `put` or `delete`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from app.identity.plex import map_path


class ArrUnavailable(RuntimeError):
    pass


@dataclass
class ArrMatch:
    path: str
    kind: str  # "movie" | "episode"
    title_kind: str  # "movie" | "show"
    external_id: str
    name: str
    year: int | None
    season: int | None
    episode: int | None


class ArrClient:
    def __init__(
        self, url: str, api_key: str, *, timeout: float = 30.0,
        path_map: dict[str, str] | None = None,
    ):
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.path_map = path_map or {}

    def _get(self, endpoint: str, **params: Any) -> Any:
        url = f"{self.base}/api/v3/{endpoint}"
        try:
            response = httpx.get(
                url, params=params or None,
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise ArrUnavailable(f"{self.base}: API key rejected") from exc
            raise ArrUnavailable(f"{self.base}/{endpoint}: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ArrUnavailable(f"{self.base}/{endpoint}: {exc}") from exc

    def ping(self) -> str:
        status = self._get("system/status")
        return f"{status.get('appName', '?')} {status.get('version', '?')}"


class RadarrClient(ArrClient):
    def matches(self) -> list[ArrMatch]:
        """One call: /movie embeds each movie's file path."""
        out: list[ArrMatch] = []
        for movie in self._get("movie"):
            movie_file = movie.get("movieFile") or {}
            path = movie_file.get("path")
            if not path:
                continue
            tmdb = movie.get("tmdbId")
            out.append(ArrMatch(
                path=map_path(path, self.path_map),
                kind="movie", title_kind="movie",
                external_id=f"tmdb:{tmdb}" if tmdb else f"radarr:{movie.get('id')}",
                name=movie.get("title") or "",
                year=movie.get("year") or None,
                season=None, episode=None,
            ))
        return out


class SonarrClient(ArrClient):
    def matches(self, series_ids: Iterable[int] | None = None) -> list[ArrMatch]:
        """Two calls per series: the files, and the episodes that point at them.

        /episodefile carries the path but only the season number; the episode
        number lives on /episode. Joining them locally is one round trip per
        series rather than one per episode.
        """
        out: list[ArrMatch] = []
        series_list = self._get("series")
        wanted = set(series_ids) if series_ids is not None else None

        for series in series_list:
            series_id = series.get("id")
            if wanted is not None and series_id not in wanted:
                continue
            tvdb = series.get("tvdbId")
            external_id = f"tvdb:{tvdb}" if tvdb else f"sonarr:{series_id}"
            name = series.get("title") or ""
            year = series.get("year") or None

            try:
                episodes = self._get("episode", seriesId=series_id)
            except ArrUnavailable:
                # One unreachable series should not abandon the whole library.
                continue

            by_file: dict[int, dict] = {}
            for episode in episodes:
                file_id = episode.get("episodeFileId")
                if not file_id:
                    continue
                # Multi-episode files map several episodes to one file; the
                # lowest episode number is the one we group on, matching what
                # the filename parser does with S01E01E02.
                current = by_file.get(file_id)
                if current is None or (episode.get("episodeNumber") or 0) < (
                    current.get("episodeNumber") or 0
                ):
                    by_file[file_id] = episode

            try:
                files = self._get("episodefile", seriesId=series_id)
            except ArrUnavailable:
                continue

            for entry in files:
                path = entry.get("path")
                episode = by_file.get(entry.get("id"))
                if not path or episode is None:
                    continue
                out.append(ArrMatch(
                    path=map_path(path, self.path_map),
                    kind="episode", title_kind="show",
                    external_id=external_id, name=name, year=year,
                    season=episode.get("seasonNumber"),
                    episode=episode.get("episodeNumber"),
                ))
        return out


def load_radarr(config) -> dict[str, ArrMatch]:
    if not config.enabled or not config.url or not config.api_key:
        return {}
    client = RadarrClient(
        config.url, config.api_key,
        timeout=config.timeout, path_map=config.path_map,
    )
    return {m.path: m for m in client.matches()}


def load_sonarr(config) -> dict[str, ArrMatch]:
    if not config.enabled or not config.url or not config.api_key:
        return {}
    client = SonarrClient(
        config.url, config.api_key,
        timeout=config.timeout, path_map=config.path_map,
    )
    return {m.path: m for m in client.matches()}
