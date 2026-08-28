"""Telling the *arrs a file changed, and letting them rename it.

The split is the design: a rescan changes nothing on disk and the worker does
it by itself; a rename changes library filenames Plex has indexed and never
happens without being asked.
"""

import json

import httpx
import pytest

from app.config import Config
from app.db import Database
from app.guard import arr_guard, arr_notify


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "notify.db")


@pytest.fixture
def config(tmp_path):
    config = Config(config_dir=tmp_path)
    for service in ("sonarr", "radarr"):
        settings = getattr(config, service)
        settings.enabled = True
        settings.url = f"http://{service}:8989"
        settings.api_key = "key"
        # What the live instances actually report: the *arr's own container
        # path, not the NAS host path.
        settings.path_map = {"/data/media": "/media"}
    return config


class FakeArr:
    """A Sonarr that answers /series, /command and /rename."""

    def __init__(self, *, series=None, renames=None, command_state="completed"):
        self.series = series if series is not None else [
            {"id": 5, "title": "The Show", "path": "/data/media/tv/The Show"},
            {"id": 6, "title": "Other", "path": "/data/media/tv/Other"},
        ]
        self.renames = renames or []
        self.command_state = command_state
        self.commands: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v3/", "")
        body = json.loads(request.content) if request.content else None

        if path == "system/status":
            return httpx.Response(200, json={"appName": "Sonarr", "version": "4.0"})
        if path == "series":
            return httpx.Response(200, json=self.series)
        if path == "movie":
            return httpx.Response(200, json=self.series)
        if path == "command" and request.method == "POST":
            self.commands.append(body)
            return httpx.Response(201, json={"id": 99, "status": "queued"})
        if path.startswith("command/"):
            return httpx.Response(200, json={"id": 99, "status": self.command_state})
        if path == "rename":
            return httpx.Response(200, json=self.renames)
        return httpx.Response(404, text=f"no route for {path}")


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(arr_notify, "RESCAN_POLL_S", 0.0)
    monkeypatch.setattr(arr_notify.time, "sleep", lambda s: None)


def wire(fake: FakeArr, service="sonarr") -> arr_guard.ArrWriter:
    transport = httpx.MockTransport(fake.handler)

    def request(method, url, **kwargs):
        with httpx.Client(transport=transport) as http:
            return http.request(method, url, **kwargs)

    import app.identity.arr as arr_module

    arr_guard.httpx.request = request
    arr_module.httpx.get = lambda url, **kw: request("GET", url, **kw)
    return arr_guard.GUARDS[service](f"http://{service}:8989", "key")


@pytest.fixture(autouse=True)
def restore_httpx():
    import app.identity.arr as arr_module

    real_request, real_get = httpx.request, arr_module.httpx.get
    yield
    httpx.request = real_request
    arr_module.httpx.get = real_get


class TestLocating:
    def test_a_file_is_matched_to_its_series_through_the_path_map(self, config):
        client = wire(FakeArr())
        item = arr_notify.locate(
            client, "sonarr", "/media/tv/The Show/Season 1/ep.mkv",
            config.sonarr.path_map,
        )
        assert item is not None and item.item_id == 5

    def test_a_file_outside_every_folder_matches_nothing(self, config):
        client = wire(FakeArr())
        assert arr_notify.locate(
            client, "sonarr", "/media/movies/Some Film/film.mkv",
            config.sonarr.path_map,
        ) is None

    def test_the_innermost_folder_wins(self, config):
        """A nested layout should resolve to the specific item, not whichever
        parent happened to be listed first."""
        client = wire(FakeArr(series=[
            {"id": 1, "title": "Outer", "path": "/data/media/tv"},
            {"id": 2, "title": "Inner", "path": "/data/media/tv/The Show"},
        ]))
        item = arr_notify.locate(
            client, "sonarr", "/media/tv/The Show/ep.mkv", config.sonarr.path_map
        )
        assert item.title == "Inner"

    def test_a_wrong_path_map_finds_nothing_rather_than_the_wrong_thing(self, config):
        """The failure mode the example config used to have: mapping the NAS
        host path instead of the *arr's own container path."""
        client = wire(FakeArr())
        assert arr_notify.locate(
            client, "sonarr", "/media/tv/The Show/ep.mkv",
            {"/volume1/data/media": "/media"},
        ) is None


class TestRescan:
    def test_it_posts_the_right_command_and_waits(self, config):
        fake = FakeArr()
        client = wire(fake)
        item = arr_notify.locate(
            client, "sonarr", "/media/tv/The Show/ep.mkv", config.sonarr.path_map
        )
        state = arr_notify.rescan(client, "sonarr", item)

        assert fake.commands == [{"name": "RescanSeries", "seriesId": 5}]
        assert state == "completed"

    def test_radarr_gets_its_own_command_name(self, config):
        fake = FakeArr(series=[
            {"id": 7, "title": "A Film", "path": "/data/media/movies/A Film"},
        ])
        client = wire(fake, "radarr")
        item = arr_notify.locate(
            client, "radarr", "/media/movies/A Film/film.mkv", config.radarr.path_map
        )
        arr_notify.rescan(client, "radarr", item)
        assert fake.commands == [{"name": "RescanMovie", "movieId": 7}]

    def test_nothing_happens_unless_it_was_switched_on(self, config, monkeypatch):
        fake = FakeArr()
        monkeypatch.setattr(arr_guard, "client_for", lambda s, c: wire(fake))
        monkeypatch.setattr(arr_notify, "client_for", lambda s, c: wire(fake))

        assert config.sonarr.notify_on_replace is False
        result = arr_notify.notify_replaced(config, "/media/tv/The Show/ep.mkv")

        assert result.rescanned == [] and fake.commands == []

    def test_switched_on_it_rescans(self, config, monkeypatch):
        fake = FakeArr()
        config.sonarr.notify_on_replace = True
        monkeypatch.setattr(arr_notify, "client_for",
                            lambda s, c: wire(fake) if s == "sonarr" else None)

        result = arr_notify.notify_replaced(config, "/media/tv/The Show/ep.mkv")

        assert len(result.rescanned) == 1
        assert fake.commands == [{"name": "RescanSeries", "seriesId": 5}]

    def test_an_unreachable_arr_never_fails_a_finished_job(self, config, monkeypatch):
        """By the time this runs the encode is verified and the file installed.
        A sulking Sonarr is a warning, not a failure."""
        config.sonarr.notify_on_replace = True

        def explode(request):
            raise httpx.ConnectError("no route to host")

        def broken_client(service, cfg):
            transport = httpx.MockTransport(explode)
            import app.identity.arr as arr_module
            arr_module.httpx.get = lambda url, **kw: httpx.Client(
                transport=transport
            ).request("GET", url, **kw)
            return arr_guard.SonarrGuard("http://sonarr:8989", "key")

        monkeypatch.setattr(arr_notify, "client_for", broken_client)
        result = arr_notify.notify_replaced(config, "/media/tv/The Show/ep.mkv")

        assert result.rescanned == []
        assert result.errors and "sonarr" in result.errors[0]


class TestRename:
    RENAME_ROW = {
        "seriesId": 5, "episodeFileId": 42,
        "existingPath": "Season 1/The Show - S01E01 [WEBDL-1080p][x264].mkv",
        "newPath": "Season 1/The Show - S01E01 [WEBDL-1080p][HEVC].mkv",
    }

    def test_the_preview_comes_from_the_arr_itself(self, config):
        fake = FakeArr(renames=[self.RENAME_ROW])
        client = wire(fake)
        item = arr_notify.locate(
            client, "sonarr", "/media/tv/The Show/ep.mkv", config.sonarr.path_map
        )
        proposals = arr_notify.rename_preview(client, "sonarr", item)

        assert len(proposals) == 1
        assert proposals[0].file_id == 42
        assert "HEVC" in proposals[0].new_path
        # A preview posts no commands.
        assert fake.commands == []

    def test_applying_renames_only_the_files_named(self, config):
        fake = FakeArr(renames=[self.RENAME_ROW])
        client = wire(fake)
        item = arr_notify.locate(
            client, "sonarr", "/media/tv/The Show/ep.mkv", config.sonarr.path_map
        )
        arr_notify.rename_apply(client, "sonarr", item, [42])

        assert fake.commands == [
            {"name": "RenameFiles", "seriesId": 5, "files": [42]}
        ]

    def test_only_items_we_have_touched_are_considered(self, db, config):
        """Someone else's badly-named library is not ours to tidy."""
        fake = FakeArr()
        client = wire(fake)
        db.execute(
            "INSERT INTO outcome (job_id, file_path, action, before_bytes, "
            "after_bytes, saved_bytes, completed_at) "
            "VALUES (NULL, '/media/tv/The Show/Season 1/ep.mkv', 'encode', 2, 1, 1, 1)"
        )
        items = arr_notify.items_we_have_touched(db, config, "sonarr", client)

        assert [i.item_id for i in items] == [5]
