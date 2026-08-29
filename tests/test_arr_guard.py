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


class TestSpottingOtherPeoplesHevcRules:
    """Deciding whether someone else's format is about HEVC.

    The patterns below are the real ones, copied from a live Sonarr 4.0.19 and
    Radarr 6.3.0. The first version of this check tested whether the regex
    *text* contained "265" or "hevc" and got BR-DISK wrong, which would have
    sent someone to `--neutralise` on a rule that exists to stop 40GB disc rips
    being downloaded.
    """

    BR_DISK = (
        r"^(?!.*\b((?<!HD[._ -]|HD)DVD|BDRip|720p|MKV|XviD|WMV|d3g|(BD)?REMUX"
        r"|^(?=.*1080p)(?=.*HEVC)|[xh][-_. ]?26[45]|German.*[DM]L"
        r"|((?<=\d{4}).*German.*([DM]L)?)(?=.*\b(AVC|HEVC|VC[-_. ]?1|MVC"
        r"|MPEG[-_. ]?2)\b))\b)(((?=.*\b(Blu[-_. ]?ray|BD|HD[-_. ]?DVD)\b)"
        r"(?=.*\b(AVC|HEVC|VC[-_. ]?1|MVC|MPEG[-_. ]?2|BDMV|ISO)\b))).*"
    )
    X265_HD = r"[xh][ ._-]?265|\bHEVC(\b|\d)"

    def fmt(self, name, pattern, *, negate=False, identifier=1):
        return {
            "id": identifier, "name": name,
            "specifications": [{
                "implementation": arr_guard.RELEASE_TITLE_SPEC,
                "negate": negate,
                "fields": [{"name": "value", "value": pattern}],
            }],
        }

    def test_a_real_x265_rule_is_recognised(self):
        assert arr_guard._hevc_verdict(self.fmt("x265 (HD)", self.X265_HD)) is True

    def test_br_disk_is_not_an_hevc_rule(self):
        """It names HEVC only inside a negative lookahead: it is about full
        disc images and deliberately excludes HEVC."""
        assert arr_guard._hevc_verdict(self.fmt("BR-DISK", self.BR_DISK)) is not True

    def test_a_release_group_list_is_not_an_hevc_rule(self):
        """An anime low-quality-group list matched the old text check because
        one group's name contains those digits."""
        groups = self.fmt("Anime LQ Groups", r"\b(Hevc-Raws)\b")
        groups["specifications"].append({
            "implementation": arr_guard.RELEASE_TITLE_SPEC,
            "negate": False,
            "fields": [{"name": "value", "value": r"\b(x265Encoders)\b"}],
        })
        assert arr_guard._hevc_verdict(groups) is False

    def test_a_negated_spec_does_not_make_it_an_hevc_rule(self):
        assert arr_guard._hevc_verdict(
            self.fmt("Not x265", self.X265_HD, negate=True)
        ) is False

    def test_a_pattern_python_cannot_compile_is_unknown_not_a_guess(self):
        """.NET allows variable-length lookbehind; Python does not. Guessing
        "yes" is the answer that does damage."""
        assert arr_guard._hevc_verdict(
            self.fmt("Odd", r"(?<!HD[._ -]|HD)DVD")
        ) is None

    def test_unreadable_formats_are_reported_rather_than_dropped(self, config):
        fake = FakeArr(formats=[self.fmt("Odd", r"(?<!HD[._ -]|HD)DVD", identifier=9)])
        result = arr_guard.plan(client_for(fake), config.guard)
        assert any("cannot evaluate" in note for note in result.notes)

    def test_only_a_real_hevc_penalty_is_warned_about(self, config):
        fake = FakeArr(
            formats=[
                self.fmt("BR-DISK", self.BR_DISK, identifier=2),
                self.fmt("x265 (HD)", self.X265_HD, identifier=3),
            ],
            profiles=[{
                "id": 1, "name": "HD-1080p",
                "formatItems": [
                    {"format": 2, "name": "BR-DISK", "score": -10000},
                    {"format": 3, "name": "x265 (HD)", "score": -10000},
                ],
            }],
        )
        result = arr_guard.plan(client_for(fake), config.guard)
        warning = " ".join(result.warnings)
        assert "x265 (HD)" in warning
        assert "BR-DISK" not in warning

    def test_neutralise_leaves_a_non_hevc_penalty_alone(self, config):
        """Raising BR-DISK to zero would let full disc images be downloaded."""
        fake = FakeArr(
            formats=[
                self.fmt("BR-DISK", self.BR_DISK, identifier=2),
                self.fmt("x265 (HD)", self.X265_HD, identifier=3),
            ],
            profiles=[{
                "id": 1, "name": "HD-1080p",
                "formatItems": [
                    {"format": 2, "name": "BR-DISK", "score": -10000},
                    {"format": 3, "name": "x265 (HD)", "score": -10000},
                ],
            }],
        )
        result = arr_guard.plan(client_for(fake), config.guard, neutralise=True)
        profile = next(c for c in result.changes if c.kind == "qualityprofile")
        scores = {i["name"]: i["score"] for i in profile.after["formatItems"]}
        assert scores["BR-DISK"] == -10000
        assert scores["x265 (HD)"] == 0


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
        assert any("does not say our own files are covered" in n
                   for n in result.notes)

    def test_the_predictive_number_is_measured_on_encode_candidates(self, config):
        """Existing HEVC files are HEVC because they were downloaded that way,
        so their names say so and the match rate looks perfect. The number that
        predicts anything is the one taken over the H.264 files we would
        re-encode, whose names our output inherits."""
        fake = FakeArr(files=[
            episode_file(scene_name="Show.S01E01.1080p.WEB-DL.x265-GRP", codec="hevc"),
            episode_file(scene_name="Show.S01E02.1080p.WEB-DL.x264-GRP", codec="h264"),
            episode_file(scene_name="Show.S01E03.1080p.WEB-DL.x264-GRP", codec="h264"),
            episode_file(scene_name="Show.S01E04.1080p.HEVC.REMUX-GRP", codec="h264"),
        ])
        result = arr_guard.plan(client_for(fake), config.guard)

        assert result.coverage.match_pct == 100.0        # reassuring, and hollow
        assert result.coverage.candidates == 3
        assert result.coverage.candidates_matched == 1   # only the oddly-named one
        assert any("THE NUMBER THAT MATTERS" in n for n in result.notes)
        assert any("2 in 3 of the files we encode" in n for n in result.notes)

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

    def test_the_configured_timeout_reaches_the_client(self, config):
        """Measured on the DS1019+ 2026-08-30: Sonarr's /series takes 46s and
        Radarr's /movie 27s, both whole-library calls. The old hardcoded 30s
        default failed the first outright and the second intermittently, and
        the failure reads as an outage rather than as slowness."""
        config.sonarr.timeout = 240.0
        assert arr_guard.client_for("sonarr", config).timeout == 240.0

    def test_identity_gets_the_timeout_too(self, config, monkeypatch):
        """Phase 1 builds its own clients, so wiring the guard alone would
        leave `app phase1` on the default against the same slow calls."""
        import app.identity.arr as arr_module

        seen = {}

        class Recorder(arr_module.SonarrClient):
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)
                super().__init__(*args, **kwargs)

            def matches(self, *args, **kwargs):
                return []

        monkeypatch.setattr(arr_module, "SonarrClient", Recorder)
        config.sonarr.timeout = 240.0
        arr_module.load_sonarr(config.sonarr)
        assert seen["timeout"] == 240.0
