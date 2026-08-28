"""The quality ladder, as production reads it.

`bench.ladder` writes profiles.yaml; this loads it back and answers the one
question the planner asks: *for this content class at this resolution, what
setting do we use, how big will the output be, and how fast will it encode?*

When profiles.yaml does not exist yet the planner can still run against a
**provisional** ladder built from the policy target bitrates. That is useful --
it produces a readable plan before the benchmark finishes -- but every estimate
it makes is a guess, so provisional decisions are written in their own state
and Phase 3 must never execute them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# Encode speeds to assume when nothing has been measured on this box yet.
# Deliberately pessimistic: a plan that over-promises throughput is worse than
# one that under-promises, because the whole point of the ranking is to spend a
# scarce resource well. Replaced the moment a real benchmark lands.
ASSUMED_FPS = {"2160p": 10.0, "1080p": 25.0, "720p": 50.0, "sd": 100.0}

# Content classes the ladder is built for (see bench/__main__.py).
CONTENT_CLASSES = ("movie", "tv")


@dataclass(frozen=True)
class Rung:
    """One row of the ladder: an encoder setting and what it is expected to do."""

    encoder: str
    content_class: str
    resolution: str
    target_vmaf: float
    quality: float
    quality_flag: str
    expected_size_ratio: float
    expected_fps: float | None = None
    # Measured output bitrate at this setting. Absent in ladders written before
    # this field existed -- the planner falls back to the policy target then.
    expected_out_bitrate: int | None = None
    extrapolated: bool = False
    samples: int = 0
    note: str = ""

    @property
    def is_hardware(self) -> bool:
        return self.encoder.endswith(("_vaapi", "_qsv"))

    @property
    def label(self) -> str:
        return f"{self.encoder} {self.quality_flag}{self.quality:g} @{self.resolution}"


class Ladder:
    """A loaded profiles.yaml, or a provisional stand-in for one."""

    def __init__(
        self,
        rungs: list[Rung],
        *,
        preferred_encoder: str | None = None,
        hw_decode: dict[str, bool] | None = None,
        provisional: bool = False,
        source: str = "",
        run_id: str | None = None,
    ):
        self.rungs = rungs
        self.preferred_encoder = preferred_encoder
        self.hw_decode = hw_decode or {}
        self.provisional = provisional
        self.source = source
        self.run_id = run_id

    # -- lookup -------------------------------------------------------------

    def rung_for(
        self, content_class: str, resolution: str, *, prefer_software: bool = False
    ) -> Rung | None:
        """The setting to use, or None if the benchmark found nothing usable.

        Prefers the encoder the benchmark nominated, then any other hardware
        encoder, then software. Software is last because on a J3455 it is hours
        per file -- valid, but only ever for a hand-picked keepers list.

        `prefer_software` inverts that for a file on that list. It does not
        *require* software: a keeper still gets encoded on the iGPU if the
        benchmark was never run with `--include-software` and there is no crf
        rung to use. Refusing to plan the file at all would be a worse answer
        than encoding it well but not perfectly.
        """
        matches = [
            r for r in self.rungs
            if r.resolution == resolution and r.content_class == content_class
        ]
        if not matches:
            return None

        def rank(rung: Rung) -> tuple[int, int]:
            if prefer_software:
                return (0 if not rung.is_hardware else 1, -rung.samples)
            if self.preferred_encoder and rung.encoder == self.preferred_encoder:
                return (0, -rung.samples)
            return (1 if rung.is_hardware else 2, -rung.samples)

        return sorted(matches, key=rank)[0]

    def has_software(self) -> bool:
        return any(not r.is_hardware for r in self.rungs)

    def unusable_at(self, resolution: str) -> str | None:
        """Why this resolution has no workable setting, if the benchmark said so.

        `bench.ladder` emits a content_class of "unusable" when no tested
        setting shrank the file at all. That is a real answer -- "do not
        re-encode this class of source" -- and the planner must repeat it
        rather than picking the least-bad rung.
        """
        for rung in self.rungs:
            if rung.resolution == resolution and rung.content_class == "unusable":
                return rung.note or "the benchmark found no setting that shrinks these files"
        return None

    def notes(self) -> list[str]:
        """Warnings worth surfacing at the top of a plan."""
        out: list[str] = []
        if self.provisional:
            out.append(
                "PROVISIONAL: no profiles.yaml, so every size and time below is a "
                "guess from the policy targets. Run `bench` and re-plan before "
                "acting on any of it."
            )
        for rung in self.rungs:
            if rung.note:
                out.append(f"{rung.encoder} {rung.resolution}: {rung.note}")
        return out


def load_ladder(path: Path | str) -> Ladder | None:
    """Read profiles.yaml. Returns None if it has not been generated yet."""
    path = Path(path)
    if not path.exists():
        return None

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rungs: list[Rung] = []
    for entry in doc.get("profiles") or []:
        rungs.append(
            Rung(
                encoder=entry.get("encoder", ""),
                content_class=entry.get("content_class", ""),
                resolution=entry.get("resolution", ""),
                target_vmaf=float(entry.get("target_vmaf") or 0.0),
                quality=float(entry.get("quality") or 0.0),
                quality_flag=entry.get("quality_flag") or "q",
                expected_size_ratio=float(entry.get("expected_size_ratio") or 0.0),
                expected_fps=entry.get("expected_fps"),
                expected_out_bitrate=entry.get("expected_out_bitrate"),
                extrapolated=bool(entry.get("extrapolated")),
                samples=int(entry.get("samples") or 0),
                note=entry.get("note") or "",
            )
        )
    return Ladder(
        rungs,
        preferred_encoder=doc.get("preferred_encoder"),
        hw_decode=doc.get("hw_decode") or {},
        provisional=False,
        source=str(path),
        run_id=doc.get("run_id"),
    )


def provisional_ladder(policy, encoder: str = "hevc_vaapi") -> Ladder:
    """A ladder invented from the policy target bitrates.

    Lets `app plan` produce a readable dry run before the benchmark has ever
    been run. The quality value is a placeholder, not a recommendation -- these
    rungs exist to make the *ranking* visible, not to be encoded with.
    """
    targets = {
        "2160p": policy.target_bitrate_1080p,   # 4K SDR is downscaled to 1080p
        "1080p": policy.target_bitrate_1080p,
        "720p": policy.target_bitrate_720p,
        "sd": policy.target_bitrate_sd,
    }
    rungs = [
        Rung(
            encoder=encoder,
            content_class=content_class,
            resolution=resolution,
            target_vmaf=0.0,
            quality=0.0,
            quality_flag="qp",
            expected_size_ratio=0.0,
            expected_fps=ASSUMED_FPS.get(resolution),
            expected_out_bitrate=bitrate,
            extrapolated=True,
            samples=0,
            note="",
        )
        for resolution, bitrate in targets.items()
        for content_class in CONTENT_CLASSES
    ]
    return Ladder(
        rungs, preferred_encoder=encoder, provisional=True, source="provisional",
    )


def resolve_ladder(config) -> Ladder:
    """The real ladder if it exists, otherwise a provisional one."""
    return load_ladder(config.profiles_path) or provisional_ladder(config.policy)
