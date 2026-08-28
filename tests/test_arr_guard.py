"""The *arr guard: what it would write, what it refuses to write, and undo.

Every test here drives a fake Sonarr through httpx's MockTransport rather than
a mock of our own client, so the request shapes -- verbs, paths, bodies -- are
exercised as the *arr would see them.
"""

import json

import httpx
import pytest

from app.config import Config
from app.db import Database
from app.guard import arr_guard
from app.identity.arr import ArrUnavailable


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "guard.db")


@pytest.fixture
def config(tmp_path):
    config = Config(config_dir=tmp_path)
    config.sonarr.enabled = True
    config.sonarr.url = "http://sonarr:8989"
    config.sonarr.api_key = "key"
    return config


HEVC_FORMAT_ID = 7


class FakeArr:
    """A Sonarr with a handful of formats, profiles and files.

    Writes land back in the same state the reads come from, so a test can plan,
    apply, and plan again to check the second pass sees the first.
    """

    def __init__(self, *, formats=None, profiles=None, files=None, schema=True):
        self.formats = formats if formats is not None else []
        self.profiles = profiles if profiles is not None else [
            {"id": 1, "name": "HD-1080p", "formatItems": [], "cutoffFormatScore": 0}
        ]
        self.files = files if files is not None else []
        self.schema = schema
        self.requests: list[tuple[str, str]] = []
        self.next_id = 20

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v3/", "")
        self.requests.append((request.method, path))
        body = json.loads(request.content) if request.content else None

        if path == "system/status":
            return httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.0"})
        if path == "customformat/schema":
            if not self.schema:
                return httpx.Response(200, json=[{"implementation": "SomethingElse"}])
            return httpx.Response(200, json=[
                {"implementation": arr_guard.RELEASE_TITLE_SPEC},
                {"implementation": "SizeSpecification"},
            ])
        if path == "customformat":
            if request.method == "POST":
                created = dict(body, id=self.next_id)
                self.next_id += 1
                self.formats.append(created)
                return httpx.Response(201, json=created)
            return httpx.Response(200, json=self.formats)
        if path.startswith("customformat/"):
            identifier = int(path.rsplit("/", 1)[-1])
            if request.method == "DELETE":
                self.formats = [f for f in self.formats if f.get("id") != identifier]
                return httpx.Response(200)
            self.formats = [
                dict(body, id=identifier) if f.get("id") == identifier else f
                for f in self.formats
            ]
            return httpx.Response(200, json=dict(body, id=identifier))
        if path == "qualityprofile":
            return httpx.Response(200, json=self.profiles)
        if path.startswith("qualityprofile/"):
            identifier = int(path.rsplit("/", 1)[-1])
            self.profiles = [
                body if p.get("id") == identifier else p for p in self.profiles
            ]
            return httpx.Response(200, json=body)
        if path == "series":
            return httpx.Response(200, json=[
                {"id": 1, "statistics": {"episodeFileCount": len(self.files)}}
            ])
        if path == "episodefile":
            return httpx.Response(200, json=self.files)
        return httpx.Response(404, text=f"no route for {path}")


def client_for(fake: FakeArr) -> arr_guard.SonarrGuard:
    client = arr_guard.SonarrGuard("http://sonarr:8989", "key")
    transport = httpx.MockTransport(fake.handler)

    def request(method, url, **kwargs):
        with httpx.Client(transport=transport) as http:
            return http.request(method, url, **kwargs)

    def get(url, **kwargs):
        return request("GET", url, **kwargs)

    # Patch at the module level the client actually calls through.
    arr_guard.httpx.request = request  # type: ignore[assignment]
    import app.identity.arr as arr_module

    arr_module.httpx.get = get  # type: ignore[assignment]
    return client


@pytest.fixture(autouse=True)
def restore_httpx():
    import app.identity.arr as arr_module

    real_request, real_get = httpx.request, arr_module.httpx.get
    yield
    httpx.request = real_request
    arr_module.httpx.get = real_get


def episode_file(scene_name="", relative_path="Show - S01E01.mkv", codec="x264",
                 formats=None):
    return {
        "sceneName": scene_name,
        "relativePath": relative_path,
        "mediaInfo": {"videoCodec": codec},
        "customFormats": [{"name": n} for n in (formats or [])],
    }


# ------------------------------------------------------------------ planning


class TestPlan:
    def test_creates_the_format_and_scores_it_on_every_profile(self, config):
        fake = FakeArr(profiles=[
            {"id": 1, "name": "HD-1080p", "formatItems": []},
            {"id": 2, "name": "Any", "formatItems": []},
        ])
        result = arr_guard.plan(client_for(fake), config.guard)

        assert result.reachable and not result.error
        kinds = [c.kind for c in result.changes]
        assert kinds.count("customformat") == 1
        assert kinds.count("qualityprofile") == 2
        assert all(c.method == "PUT" for c in result.changes if c.kind == "qualityprofile")

    def test_the_score_is_positive(self, config):
        """The whole point. A negative score would make every H.264 release an
        upgrade over the file we just spent the night making."""
        fake = FakeArr()
        result = arr_guard.plan(client_for(fake), config.guard)
        profile = next(c for c in result.changes if c.kind == "qualityprofile")
        item = next(
            i for i in profile.after["formatItems"]
            if i["name"] == config.guard.format_name
        )
        assert item["score"] > 0

    def test_nothing_to_do_when_it_is_already_installed(self, config):
        wanted = dict(arr_guard.desired_format(config.guard), id=HEVC_FORMAT_ID)
        fake = FakeArr(
            formats=[wanted],
            profiles=[{
                "id": 1, "name": "HD-1080p",
                "formatItems": [{
                    "format": HEVC_FORMAT_ID,
                    "name": config.guard.format_name,
                    "score": config.guard.score,
                }],
            }],
        )
        result = arr_guard.plan(client_for(fake), config.guard)
        assert result.clean

    def test_an_unrecognised_schema_stops_rather_than_guesses(self, config):
        fake = FakeArr(schema=False)
        result = arr_guard.plan(client_for(fake), config.guard)
        assert result.reachable
        assert not result.changes
        assert arr_guard.RELEASE_TITLE_SPEC in result.error

    def test_an_existing_negative_hevc_score_is_a_warning(self, config):
        """TRaSH's "x265 (HD)" at -10000 is a common, deliberate setting, and
        while it stands nothing the guard adds protects anything."""
        rival = {
            "id": 3, "name": "x265 (HD)",
            "specifications": [{
                "implementation": arr_guard.RELEASE_TITLE_SPEC,
                "fields": [{"name": "value", "value": "(x265|hevc)"}],
            }],
        }
        fake = FakeArr(
            formats=[rival],
            profiles=[{
                "id": 1, "name": "HD-1080p",
                "formatItems": [{"format": 3, "name": "x265 (HD)", "score": -10000}],
            }],
        )
        result = arr_guard.plan(client_for(fake), config.guard)
        assert any("penalises HEVC" in w for w in result.warnings)
        # Not touched without being asked: it is the user's own tuning.
        profile = next(c for c in result.changes if c.kind == "qualityprofile")
        rival_item = next(i for i in profile.after["formatItems"] if i["format"] == 3)
        assert rival_item["score"] == -10000

    def test_neutralise_raises_the_rival_score_to_zero(self, config):
        rival = {
            "id": 3, "name": "x265 (HD)",
            "specifications": [{
                "implementation": arr_guard.RELEASE_TITLE_SPEC,
                "fields": [{"name": "value", "value": "(x265|hevc)"}],
            }],
        }
        fake = FakeArr(
            formats=[rival],
            profiles=[{
                "id": 1, "name": "HD-1080p",
                "formatItems": [{"format": 3, "name": "x265 (HD)", "score": -10000}],
            }],
        )
        result = arr_guard.plan(client_for(fake), config.guard, neutralise=True)
        profile = next(c for c in result.changes if c.kind == "qualityprofile")
        rival_item = next(i for i in profile.after["formatItems"] if i["format"] == 3)
        assert rival_item["score"] == 0
        assert not result.warnings

    def test_unreachable_is_reported_not_raised(self, config):
        def refuse(request):
            return httpx.Response(401)

        client = arr_guard.SonarrGuard("http://sonarr:8989", "bad")
        import app.identity.arr as arr_module

        transport = httpx.MockTransport(refuse)
        arr_module.httpx.get = lambda url, **kw: httpx.Client(
            transport=transport
        ).request("GET", url, **kw)

        result = arr_guard.plan(client, config.guard)
        assert not result.reachable
        assert "API key rejected" in result.error


# ------------------------------------------------------------------ coverage


class TestCoverage:
    def test_counts_hevc_files_the_regex_would_actually_reach(self, config):
        fake = FakeArr(files=[
            episode_file(scene_name="Show.S01E01.1080p.WEB-DL.x265-GRP", codec="hevc"),
            episode_file(scene_name="Show.S01E02.1080p.WEB-DL.x264-GRP", codec="hevc"),
            episode_file(scene_name="Show.S01E03.1080p.WEB-DL.x264-GRP", codec="h264"),
        ])
        result = arr_guard.plan(client_for(fake), config.guard)

        assert result.coverage.sampled == 3
        assert result.coverage.hevc == 2
        # The second is HEVC on disk but still named x264 -- which is exactly
        # what our own re-encode produces, and the *arr scores by the name.
        assert result.coverage.matched == 1
        assert any("will not match them" in note for note in result.notes)

    def test_falls_back_to_the_file_name_when_there_is_no_scene_name(self, config):
        fake = FakeArr(files=[
            episode_file(relative_path="Season 1/Show - S01E01 [HEVC].mkv",
                         codec="hevc"),
        ])
        result = arr_guard.plan(client_for(fake), config.guard)
        assert result.coverage.matched == 1

    def test_no_hevc_yet_is_not_treated_as_a_failure(self, config):
        fake = FakeArr(files=[episode_file(codec="h264")])
        result = arr_guard.plan(client_for(fake), config.guard)
        assert result.coverage.hevc == 0
        assert any("before any encoding has run" in note for note in result.notes)


# ------------------------------------------------------------------ applying


class TestApply:
    def test_writes_the_format_first_then_scores_it(self, db, config):
        fake = FakeArr()
        result = arr_guard.apply(db, client_for(fake), config.guard, progress=None)

        assert result.failed == 0
        assert result.applied == 2
        posted = [r for r in fake.requests if r[0] == "POST"]
        assert posted == [("POST", "customformat")]
        item = fake.profiles[0]["formatItems"][0]
        assert item["score"] == config.guard.score
        # The id the *arr assigned, not a guess -- the profile is written after
        # the format comes back.
        assert item["format"] == fake.formats[0]["id"]

    def test_running_it_twice_changes_nothing_the_second_time(self, db, config):
        fake = FakeArr()
        client = client_for(fake)
        arr_guard.apply(db, client, config.guard, progress=None)
        second = arr_guard.plan(client, config.guard)
        assert second.clean

    def test_every_write_is_recorded(self, db, config):
        fake = FakeArr()
        arr_guard.apply(db, client_for(fake), config.guard, progress=None)
        rows = db.query("SELECT * FROM guard_change ORDER BY id")
        assert [r["kind"] for r in rows] == ["customformat", "qualityprofile"]
        assert all(r["reverted_at"] is None for r in rows)
        # The POST records the response, because the id in it is what revert
        # needs to delete.
        assert json.loads(rows[0]["after_json"])["id"] == fake.formats[0]["id"]


class TestRevert:
    def test_puts_the_profile_back_and_deletes_the_format(self, db, config):
        fake = FakeArr(profiles=[{
            "id": 1, "name": "HD-1080p",
            "formatItems": [{"format": 3, "name": "Other", "score": 50}],
        }])
        client = client_for(fake)
        arr_guard.apply(db, client, config.guard, progress=None)
        assert len(fake.formats) == 1

        # revert() builds its own clients from config, so point them here too.
        config.sonarr.url = "http://sonarr:8989"
        result = arr_guard.revert(db, config, progress=None)

        assert result.failed == 0
        assert fake.formats == []
        assert fake.profiles[0]["formatItems"] == [
            {"format": 3, "name": "Other", "score": 50}
        ]
        assert all(
            r["reverted_at"] is not None
            for r in db.query("SELECT * FROM guard_change")
        )

    def test_nothing_to_revert_is_not_an_error(self, db, config):
        result = arr_guard.revert(db, config, progress=None)
        assert result.applied == 0 and result.failed == 0


# ------------------------------------------------------------------ transport


class TestWriter:
    def test_the_read_only_client_still_cannot_write(self):
        """Phase 1 resolves identity on every run through ArrClient. It should
        stay incapable of changing anything."""
        from app.identity.arr import ArrClient

        assert not hasattr(ArrClient, "post")
        assert not hasattr(ArrClient, "put")
        assert not hasattr(ArrClient, "delete")

    def test_an_http_error_becomes_ArrUnavailable(self, config):
        fake = FakeArr()
        client = client_for(fake)
        with pytest.raises(ArrUnavailable):
            client.put("nosuchthing", {})
