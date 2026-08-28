"""Stop Sonarr and Radarr deleting the files we spend all night making.

The problem, stated plainly: an *arr keeps looking for a better release of
something it already has. Our encode leaves the file at the same parsed quality
it was before -- the name has not changed, so neither has Sonarr's idea of what
it is -- and the next matching H.264 release looks like a perfectly good
upgrade. It downloads it, and it **deletes our HEVC file to make room**. Every
night of encoding gets quietly undone, and the queue is measured in months.

The lever is a custom format that matches HEVC, given a **positive** score on
every quality profile. Both *arrs decide an upgrade by comparing the candidate
release's custom-format score against the score of the file already on disk,
and reject anything that is not strictly better at the same quality. Scoring
HEVC highly is therefore what protects it. Scoring it negatively -- which reads
plausibly, since we are trying to make HEVC *not* an upgrade candidate -- does
precisely the opposite: it makes every H.264 release an upgrade over our work.
That inversion is the whole reason this module exists rather than a one-line
API call, and it is why the dry run comes first.

Three things follow from that, and all three are in the report:

- **A custom format can only match what the *arr scores the file by**, which is
  its release title (`sceneName`, or the file name when there is none). Our
  re-encode keeps the original name, and that name usually says `x264`. So
  before writing anything, the guard samples the *arr's own files, finds the
  ones it reports as HEVC, and counts how many of their names our regex would
  actually match. A guard that protects none of the files it exists for is
  worth knowing about *before* it is installed, not after a month of encoding.
- **An existing negative score beats us.** A TRaSH-style "x265 (HD)" format
  scored -10000 is a common, deliberate configuration, and while it stands
  nothing we add can protect anything. The guard finds those and says so.
  `--neutralise` raises them to zero, but only when asked: they are the user's
  own tuning.
- **A genuine quality upgrade still wins**, and should. If a profile is set to
  keep climbing towards Bluray-1080p and finds one, it replaces our WEB-DL
  however we score it. That is the user's stated intent, and the guard is not
  in the business of overriding it.

Every write is recorded in `guard_change` with the payload it replaced, so
`--revert` can put it all back.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from app.identity.arr import ArrClient, ArrUnavailable

# The specification the format is built from. Checked against the live
# /customformat/schema before anything is written -- we do not invent a shape
# and hope, and a schema we do not recognise is a stop, not a warning.
RELEASE_TITLE_SPEC = "ReleaseTitleSpecification"

# Deciding whether somebody else's custom format is about HEVC.
#
# The obvious test -- does its regex text contain "265" or "hevc" -- is wrong,
# and wrong in the expensive direction. Checked against a real Sonarr it flagged
# TRaSH's BR-DISK, whose pattern mentions HEVC only inside a *negative*
# lookahead: it is a rule about full BluRay disc images that deliberately
# excludes HEVC. Acting on that reading would have raised a -10000 that exists
# to stop 40GB disc rips being downloaded. It also flagged an anime
# release-group list, on a group whose name happens to contain those digits.
#
# So the test is semantic: run the pattern against a pair of release names that
# differ only in codec. A format is about HEVC if it matches the HEVC one and
# not the H.264 one. Group names in the probes are deliberately absurd so a
# list of release groups cannot match by accident.
HEVC_PROBES = (
    "Some.Title.2020.1080p.BluRay.x265-VIDSMASHARRPROBE",
    "Some.Title.2020.1080p.BluRay.HEVC-VIDSMASHARRPROBE",
)
H264_PROBES = (
    "Some.Title.2020.1080p.BluRay.x264-VIDSMASHARRPROBE",
    "Some.Title.2020.1080p.BluRay.AVC-VIDSMASHARRPROBE",
)

# How many series/movies to ask about when sampling files. Each is a round trip
# on Sonarr, and a few dozen is plenty to answer "does the regex match?".
MAX_SAMPLE_REQUESTS = 40


# ------------------------------------------------------------------ transport


class ArrWriter(ArrClient):
    """The read-only client plus the three verbs the guard needs.

    Deliberately a subclass rather than three more methods on `ArrClient`.
    Phase 1 resolves identity through that class on every run, and it should
    stay incapable of changing anything; the ability to write lives here, in
    the one module that has a reason for it.
    """

    def _send(self, method: str, endpoint: str, payload: Any = None) -> Any:
        url = f"{self.base}/api/v3/{endpoint}"
        try:
            response = httpx.request(
                method, url, json=payload,
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:300]
            raise ArrUnavailable(
                f"{self.base}/{endpoint}: HTTP {exc.response.status_code} {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ArrUnavailable(f"{self.base}/{endpoint}: {exc}") from exc

    def post(self, endpoint: str, payload: dict) -> Any:
        return self._send("POST", endpoint, payload)

    def put(self, endpoint: str, payload: dict) -> Any:
        return self._send("PUT", endpoint, payload)

    def delete(self, endpoint: str) -> Any:
        return self._send("DELETE", endpoint)

    # -- what the guard reads ------------------------------------------------

    def custom_formats(self) -> list[dict]:
        return self._get("customformat") or []

    def custom_format_schema(self) -> list[dict]:
        return self._get("customformat/schema") or []

    def quality_profiles(self) -> list[dict]:
        return self._get("qualityprofile") or []

    def sample_files(self, limit: int) -> Iterator["ArrFile"]:
        raise NotImplementedError


@dataclass
class ArrFile:
    """One file as the *arr sees it -- not as we see it on disk.

    `scored_name` is the string a custom format is actually matched against:
    the release name it was imported under, falling back to the file name.
    """

    scene_name: str
    relative_path: str
    video_codec: str
    score: int | None = None
    formats: list[str] = field(default_factory=list)

    @property
    def scored_name(self) -> str:
        return self.scene_name or self.relative_path.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def is_hevc(self) -> bool:
        codec = (self.video_codec or "").lower()
        return "265" in codec or "hevc" in codec


def _file_from(entry: dict) -> ArrFile:
    media = entry.get("mediaInfo") or {}
    return ArrFile(
        scene_name=entry.get("sceneName") or "",
        relative_path=entry.get("relativePath") or entry.get("path") or "",
        video_codec=media.get("videoCodec") or "",
        score=entry.get("customFormatScore"),
        formats=[f.get("name", "") for f in (entry.get("customFormats") or [])],
    )


class SonarrGuard(ArrWriter):
    service = "sonarr"

    def sample_files(self, limit: int) -> Iterator[ArrFile]:
        """Episode files, series by series.

        /episodefile needs a seriesId, so this is one round trip per series.
        Series with the most files first, so a small sample still covers the
        libraries that matter.
        """
        series = self._get("series") or []
        series.sort(
            key=lambda s: (s.get("statistics") or {}).get("episodeFileCount") or 0,
            reverse=True,
        )
        seen = 0
        for entry in series[:MAX_SAMPLE_REQUESTS]:
            if seen >= limit:
                return
            try:
                files = self._get("episodefile", seriesId=entry.get("id")) or []
            except ArrUnavailable:
                continue  # one unreachable series should not end the sample
            for item in files:
                yield _file_from(item)
                seen += 1
                if seen >= limit:
                    return


class RadarrGuard(ArrWriter):
    service = "radarr"

    def sample_files(self, limit: int) -> Iterator[ArrFile]:
        """/movie embeds each movie's file, so the whole sample is one call."""
        seen = 0
        for movie in self._get("movie") or []:
            movie_file = movie.get("movieFile")
            if not movie_file:
                continue
            yield _file_from(movie_file)
            seen += 1
            if seen >= limit:
                return


GUARDS = {"sonarr": SonarrGuard, "radarr": RadarrGuard}


def client_for(service: str, config) -> ArrWriter | None:
    """The write-capable client for one service, or None if it is not set up."""
    settings = getattr(config, service, None)
    if not settings or not settings.enabled or not settings.url or not settings.api_key:
        return None
    return GUARDS[service](
        settings.url, settings.api_key, path_map=settings.path_map
    )


# ------------------------------------------------------------------ the plan


@dataclass
class Change:
    service: str
    kind: str          # customformat | qualityprofile
    target: str        # the name a human recognises it by
    method: str        # POST | PUT
    endpoint: str
    after: dict
    before: dict | None = None
    summary: str = ""


@dataclass
class Coverage:
    """Would the format we are about to write match the files it is for?

    Two different questions live here, and only the second one predicts
    anything about *our* files:

    - `hevc` / `matched`: of the HEVC files already in the library, how many
      does the format reach? This looks reassuring and largely is not. Those
      files are HEVC because they were *downloaded* as HEVC releases, so of
      course their names say so.
    - `candidates` / `candidates_matched`: of the H.264 files -- the ones we
      would re-encode -- how many carry a name the format would match anyway?
      Our encode keeps the source name, so this is the fraction of our own
      output that ends up protected, and it is usually close to zero.
    """

    sampled: int = 0
    hevc: int = 0
    matched: int = 0
    already_scored: int = 0
    candidates: int = 0
    candidates_matched: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def match_pct(self) -> float:
        return 100.0 * self.matched / self.hevc if self.hevc else 0.0

    @property
    def candidate_match_pct(self) -> float:
        if not self.candidates:
            return 0.0
        return 100.0 * self.candidates_matched / self.candidates


@dataclass
class GuardPlan:
    service: str
    reachable: bool = False
    version: str = ""
    error: str = ""
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    coverage: Coverage | None = None

    @property
    def clean(self) -> bool:
        return self.reachable and not self.changes


def desired_format(cfg) -> dict:
    """The custom format we want to exist, in the *arr's own shape."""
    return {
        "name": cfg.format_name,
        "includeCustomFormatWhenRenaming": False,
        "specifications": [
            {
                "name": "HEVC",
                "implementation": RELEASE_TITLE_SPEC,
                "negate": False,
                "required": True,
                "fields": [{"name": "value", "value": cfg.match_regex}],
            }
        ],
    }


def _spec_values(fmt: dict) -> list[str]:
    out: list[str] = []
    for spec in fmt.get("specifications") or []:
        for field_entry in spec.get("fields") or []:
            value = field_entry.get("value")
            if isinstance(value, str):
                out.append(value)
    return out


def _targets_hevc(pattern: str) -> bool | None:
    """True, False, or None when the pattern cannot be evaluated here.

    Sonarr runs these on .NET, which allows variable-length lookbehind; Python
    does not, and TRaSH's BR-DISK pattern uses one. An unreadable pattern is
    reported as unknown rather than guessed at in either direction -- guessing
    "yes" is what would send someone to `--neutralise` on a format that has
    nothing to do with us.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    if not any(regex.search(probe) for probe in HEVC_PROBES):
        return False
    return not any(regex.search(probe) for probe in H264_PROBES)


def _hevc_verdict(fmt: dict) -> bool | None:
    """Is this format about HEVC? None if we could not tell."""
    unreadable = False
    for spec in fmt.get("specifications") or []:
        # A negated spec means the format is about everything *except* this, so
        # matching HEVC there does not make it an HEVC rule.
        if spec.get("negate"):
            continue
        for field_entry in spec.get("fields") or []:
            value = field_entry.get("value")
            if not isinstance(value, str):
                continue
            verdict = _targets_hevc(value)
            if verdict is True:
                return True
            if verdict is None:
                unreadable = True
    return None if unreadable else False


def _looks_like_hevc_format(fmt: dict) -> bool:
    return _hevc_verdict(fmt) is True


def _format_matches_wanted(existing: dict, wanted: dict) -> bool:
    """Is the existing format already the one we would write?

    Compared on what actually decides behaviour -- the specification's
    implementation and its regex -- rather than on the whole payload, which
    carries ids and defaults the *arr fills in itself.
    """
    specs = existing.get("specifications") or []
    if len(specs) != 1:
        return False
    spec = specs[0]
    if spec.get("implementation") != RELEASE_TITLE_SPEC or spec.get("negate"):
        return False
    return _spec_values(existing) == _spec_values(wanted)


def measure_coverage(client: ArrWriter, cfg) -> Coverage:
    """Sample the *arr's files and ask whether the regex would reach them."""
    pattern = re.compile(cfg.match_regex)
    coverage = Coverage()
    for entry in client.sample_files(cfg.sample_files):
        coverage.sampled += 1
        hits = bool(pattern.search(entry.scored_name))

        if not entry.is_hevc:
            # An encode candidate. Its name is what our output will inherit.
            coverage.candidates += 1
            if hits:
                coverage.candidates_matched += 1
            elif len(coverage.misses) < 5:
                coverage.misses.append(entry.scored_name)
            continue

        coverage.hevc += 1
        if cfg.format_name in entry.formats:
            coverage.already_scored += 1
        if hits:
            coverage.matched += 1
    return coverage


def _profile_change(
    profile: dict, format_id: int | None, cfg, *, neutralise_ids: dict[int, str],
) -> tuple[dict, list[str]] | None:
    """The edited profile and what changed, or None if it is already right."""
    after = json.loads(json.dumps(profile))  # a copy the *arr will accept back
    items = after.setdefault("formatItems", [])
    changed: list[str] = []

    ours = None
    for item in items:
        if (format_id is not None and item.get("format") == format_id) or (
            item.get("name") == cfg.format_name
        ):
            ours = item
        elif item.get("format") in neutralise_ids and (item.get("score") or 0) < 0:
            name = neutralise_ids[item["format"]]
            changed.append(f"{name}: {item.get('score')} -> 0")
            item["score"] = 0

    if ours is None:
        items.append({
            "format": format_id, "name": cfg.format_name, "score": cfg.score,
        })
        changed.append(f"add {cfg.format_name} at {cfg.score:+d}")
    elif (ours.get("score") or 0) != cfg.score:
        changed.append(f"{cfg.format_name}: {ours.get('score') or 0:+d} -> {cfg.score:+d}")
        ours["score"] = cfg.score

    return (after, changed) if changed else None


def plan(client: ArrWriter, cfg, *, neutralise: bool = False) -> GuardPlan:
    """What would change, and why. Reads only."""
    result = GuardPlan(service=client.service)
    try:
        result.version = client.ping()
        result.reachable = True
    except ArrUnavailable as exc:
        result.error = str(exc)
        return result

    try:
        schema = client.custom_format_schema()
        implementations = {entry.get("implementation") for entry in schema}
        if RELEASE_TITLE_SPEC not in implementations:
            result.error = (
                f"{client.service} does not offer {RELEASE_TITLE_SPEC}; this "
                f"version's custom formats are a different shape and the guard "
                f"will not guess at one"
            )
            return result

        formats = client.custom_formats()
        profiles = client.quality_profiles()
        result.coverage = measure_coverage(client, cfg)
    except ArrUnavailable as exc:
        result.error = str(exc)
        return result

    wanted = desired_format(cfg)
    ours = next((f for f in formats if f.get("name") == cfg.format_name), None)
    format_id = ours.get("id") if ours else None

    if ours is None:
        result.changes.append(Change(
            service=client.service, kind="customformat", target=cfg.format_name,
            method="POST", endpoint="customformat", after=wanted,
            summary=f"create custom format matching {cfg.match_regex}",
        ))
    elif not _format_matches_wanted(ours, wanted):
        after = dict(ours)
        after["specifications"] = wanted["specifications"]
        result.changes.append(Change(
            service=client.service, kind="customformat", target=cfg.format_name,
            method="PUT", endpoint=f"customformat/{format_id}",
            before=ours, after=after,
            summary=f"rewrite its specification to {cfg.match_regex}",
        ))

    # Other people's HEVC formats. A negative one is not a detail: while it
    # stands, our files score below a plain H.264 release and the guard cannot
    # protect anything.
    rivals: dict[int, str] = {}
    unreadable: list[str] = []
    for entry in formats:
        if entry.get("id") is None or entry.get("name") == cfg.format_name:
            continue
        verdict = _hevc_verdict(entry)
        if verdict is True:
            rivals[entry["id"]] = entry.get("name", "?")
        elif verdict is None:
            unreadable.append(entry.get("name", "?"))

    if unreadable:
        result.notes.append(
            f"{len(unreadable)} custom format(s) use regex syntax this cannot "
            f"evaluate (.NET allows variable-length lookbehind and Python does "
            f"not), so they were left out of the check above rather than "
            f"guessed at: {', '.join(sorted(unreadable)[:6])}. If the guard "
            f"turns out not to protect anything, look at those by hand."
        )
    penalised = sorted({
        f"{rivals[item['format']]} ({item.get('score')})"
        for profile in profiles
        for item in profile.get("formatItems") or []
        if item.get("format") in rivals and (item.get("score") or 0) < 0
    })
    if penalised and not neutralise:
        result.warnings.append(
            "another custom format already penalises HEVC: "
            + ", ".join(penalised)
            + ". While that score stands our files rank BELOW a fresh H.264 "
            "release and nothing here protects them. Re-run with --neutralise "
            "to raise those scores to zero, or clear them by hand."
        )

    neutralise_ids = rivals if neutralise else {}
    for profile in profiles:
        edit = _profile_change(profile, format_id, cfg, neutralise_ids=neutralise_ids)
        if edit is None:
            continue
        after, changed = edit
        result.changes.append(Change(
            service=client.service, kind="qualityprofile",
            target=profile.get("name", "?"),
            method="PUT", endpoint=f"qualityprofile/{profile.get('id')}",
            before=profile, after=after, summary="; ".join(changed),
        ))

    result.notes.extend(_coverage_notes(result.coverage, cfg))
    result.notes.append(
        "This does not stop a genuine quality upgrade -- a profile still "
        "climbing to Bluray-1080p will replace a WEB-DL HEVC file whatever it "
        "scores. That is the profile doing what it was told to."
    )
    return result


def _coverage_notes(coverage: Coverage | None, cfg) -> list[str]:
    if coverage is None or coverage.sampled == 0:
        return ["could not sample any files, so the match rate is unknown"]

    notes = []
    if coverage.hevc:
        notes.append(
            f"sampled {coverage.sampled} file(s). Of the {coverage.hevc} already "
            f"HEVC, the format matches {coverage.matched} "
            f"({coverage.match_pct:.0f}%) -- but those are HEVC because they were "
            f"downloaded as HEVC releases, so their names say so. That number "
            f"says the regex works; it does not say our own files are covered."
        )
    else:
        notes.append(
            f"sampled {coverage.sampled} file(s), none of them HEVC yet, which is "
            f"expected before any encoding has run."
        )

    if not coverage.candidates:
        return notes

    unprotected = coverage.candidates - coverage.candidates_matched
    notes.append(
        f"THE NUMBER THAT MATTERS: our encode keeps the source file name, and an "
        f"*arr scores a file by that name rather than by what is inside it. Of "
        f"the {coverage.candidates} H.264 file(s) sampled -- the ones we would "
        f"re-encode -- {coverage.candidates_matched} carry a name this format "
        f"would match ({coverage.candidate_match_pct:.0f}%). So roughly "
        f"{unprotected} in {coverage.candidates} of the files we encode would be "
        f"left unprotected by this format alone."
    )
    if unprotected:
        notes.append(
            "closing that gap means putting {MediaInfo VideoCodec} in the *arr "
            "file naming format and renaming, which rewrites library filenames "
            "Plex has already indexed -- a deliberate choice, and not one this "
            "will make for you. Names that would miss: "
            + ", ".join(coverage.misses)
        )
    return notes


# ------------------------------------------------------------------ applying


@dataclass
class ApplyResult:
    applied: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _record(db, change: Change, response: Any) -> None:
    db.execute(
        "INSERT INTO guard_change (service, kind, target, method, endpoint, "
        " before_json, after_json, summary, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            change.service, change.kind, change.target, change.method,
            change.endpoint,
            json.dumps(change.before) if change.before is not None else None,
            json.dumps(response if isinstance(response, dict) else change.after),
            change.summary, time.time(),
        ),
    )


def _execute(client: ArrWriter, change: Change) -> Any:
    if change.method == "POST":
        return client.post(change.endpoint, change.after)
    return client.put(change.endpoint, change.after)


def apply(db, client: ArrWriter, cfg, *, neutralise: bool = False,
          progress=print) -> ApplyResult:
    """Write the plan. The format goes first, because the profiles need its id.

    The plan is recomputed after the format lands rather than patched up from
    the first pass: the *arr assigns the id, and several versions also add a
    new format to every profile themselves. Asking again is both simpler and
    the only way to see what it actually did.
    """
    result = ApplyResult()
    first = plan(client, cfg, neutralise=neutralise)
    if not first.reachable:
        result.failed += 1
        result.errors.append(f"{client.service}: {first.error}")
        return result

    format_changes = [c for c in first.changes if c.kind == "customformat"]
    for change in format_changes:
        try:
            response = _execute(client, change)
            _record(db, change, response)
            result.applied += 1
            if progress:
                progress(f"    {client.service}: {change.summary}")
        except ArrUnavailable as exc:
            result.failed += 1
            result.errors.append(f"{client.service} {change.target}: {exc}")
            return result  # without the format, the profile edits are meaningless

    second = plan(client, cfg, neutralise=neutralise)
    for change in second.changes:
        if change.kind == "customformat":
            result.failed += 1
            result.errors.append(
                f"{client.service}: the custom format still needs writing after "
                f"we wrote it -- {change.summary}"
            )
            continue
        try:
            response = _execute(client, change)
            _record(db, change, response)
            result.applied += 1
            if progress:
                progress(f"    {client.service}: {change.target} -- {change.summary}")
        except ArrUnavailable as exc:
            result.failed += 1
            result.errors.append(f"{client.service} {change.target}: {exc}")

    return result


def revert(db, config, *, progress=print) -> ApplyResult:
    """Put back everything the guard has written, newest first.

    Newest-first matters: the profile scores were written after the format was
    created, so undoing in that order removes the references before removing
    the format they point at.
    """
    result = ApplyResult()
    rows = db.query(
        "SELECT * FROM guard_change WHERE reverted_at IS NULL "
        "ORDER BY applied_at DESC, id DESC"
    )
    if not rows:
        if progress:
            progress("  nothing to revert: the guard has not written anything.")
        return result

    clients: dict[str, ArrWriter | None] = {}
    for row in rows:
        service = row["service"]
        if service not in clients:
            clients[service] = client_for(service, config)
        client = clients[service]
        if client is None:
            result.failed += 1
            result.errors.append(f"{service} is not configured any more")
            continue

        try:
            if row["method"] == "POST":
                created = json.loads(row["after_json"])
                identifier = created.get("id")
                if identifier is None:
                    raise ArrUnavailable(
                        "the response we recorded has no id, so there is nothing "
                        "to delete -- remove the custom format by hand"
                    )
                client.delete(f"{row['kind']}/{identifier}")
                what = f"deleted {row['target']}"
            else:
                client.put(row["endpoint"], json.loads(row["before_json"]))
                what = f"restored {row['target']}"
            db.execute(
                "UPDATE guard_change SET reverted_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            result.applied += 1
            if progress:
                progress(f"    {service}: {what}")
        except (ArrUnavailable, json.JSONDecodeError, TypeError) as exc:
            result.failed += 1
            result.errors.append(f"{service} {row['target']}: {exc}")

    return result


# ------------------------------------------------------------------ reporting


def render(result: GuardPlan, *, verbose: bool = False) -> str:
    """The dry-run diff. What would change, in the order it would change."""
    lines = [f"  {result.service.upper()}"]
    if not result.reachable:
        lines.append(f"    unreachable: {result.error}")
        return "\n".join(lines)

    lines.append(f"    connected to {result.version}")
    if result.error:
        lines.append(f"    stopped: {result.error}")
        return "\n".join(lines)

    for warning in result.warnings:
        lines.append(f"    ! {warning}")

    if not result.changes:
        lines.append("    already as it should be; nothing to write")
    kinds = {"customformat": "custom format", "qualityprofile": "quality profile"}
    for change in result.changes:
        lines.append(
            f"    {change.method:<4} {kinds.get(change.kind, change.kind)} "
            f"\"{change.target}\""
        )
        lines.append(f"         {change.summary}")
        if verbose and change.before is not None:
            lines.append(f"         before: {json.dumps(change.before)[:400]}")
            lines.append(f"         after:  {json.dumps(change.after)[:400]}")
        elif verbose:
            lines.append(f"         body:   {json.dumps(change.after)[:400]}")

    for note in result.notes:
        lines.append(f"    - {note}")
    return "\n".join(lines)
