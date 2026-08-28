"""What should happen to one file, and why.

Pure policy: facts in, a decision out. No database, no filesystem, no ffmpeg --
which is what makes the rules cheap to test, and testing them is the point.

The rule that matters most is the first one: anything not provably 8-bit SDR is
never rewritten at all. Apollo Lake encodes HEVC in 8-bit only, so an HDR file
pushed through it comes back SDR with the grade gone and, once Phase 3 deletes
the original, gone permanently.

The ordering below is deliberate. Safety first, then "would this even help",
then "is it worth the CPU". A file rejected early never reaches an estimate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.scan.probe import resolution_tier

# Codecs already at or beyond what our hardware encoder achieves. Re-encoding
# these produces LARGER files -- observed on the first real benchmark run: a
# 720p x265 source came out at 299% of its original size at qp=20.
EFFICIENT_CODECS = {"hevc", "h265", "av1", "vp9"}

# Below these bitrates a source is already lean enough that HEVC has nothing
# left to take, and the encode would cost hours to produce a slightly worse file.
MIN_ENCODE_BITRATE = {"2160p": 8_000_000, "1080p": 2_500_000,
                      "720p": 1_200_000, "sd": 600_000}

# Actions a decision can carry.
ENCODE = "encode"        # re-encode video to HEVC in place of the source codec
DOWNSCALE = "downscale"  # re-encode and drop 4K SDR to 1080p
REMUX = "remux"          # stream copy; drop unwanted audio/subtitle tracks only
SKIP = "skip"

CPU_ACTIONS = (ENCODE, DOWNSCALE)


@dataclass
class FileFacts:
    """Everything the rules need about one file, flattened out of the database."""

    file_id: int
    path: str
    size_bytes: int
    duration_s: float
    container: str | None
    v_codec: str | None
    v_bit_depth: int | None
    v_width: int | None
    v_height: int | None
    v_bitrate: int | None
    v_fps: float | None
    hdr_type: str
    audio: list[dict[str, Any]] = field(default_factory=list)
    subs: list[dict[str, Any]] = field(default_factory=list)
    content_class: str = "tv"       # movie | tv
    title_id: int | None = None
    probed: bool = True
    in_open_duplicate_group: bool = False
    # On the hand-written x265 keepers list. Costs hours instead of minutes, so
    # nothing sets this except a path the user typed. See plan/keepers.py.
    is_keeper: bool = False

    @property
    def tier(self) -> str:
        return resolution_tier(self.v_width, self.v_height)

    @property
    def is_protected(self) -> bool:
        """Anything we cannot prove is 8-bit SDR. See the module docstring."""
        return self.hdr_type != "sdr"

    @classmethod
    def from_row(
        cls,
        row: Any,
        *,
        content_class: str = "tv",
        title_id: int | None = None,
        in_open_duplicate_group: bool = False,
        is_keeper: bool = False,
    ) -> "FileFacts":
        def parse(column: str) -> list[dict]:
            try:
                return json.loads(row[column] or "[]")
            except (json.JSONDecodeError, TypeError, IndexError, KeyError):
                return []

        return cls(
            file_id=row["id"],
            path=row["path"],
            size_bytes=row["size_bytes"] or 0,
            duration_s=row["duration_s"] or 0.0,
            container=row["container"],
            v_codec=(row["v_codec"] or "").lower() or None,
            v_bit_depth=row["v_bit_depth"],
            v_width=row["v_width"],
            v_height=row["v_height"],
            v_bitrate=row["v_bitrate"],
            v_fps=row["v_fps"],
            hdr_type=row["hdr_type"] or "unknown",
            audio=parse("audio_json"),
            subs=parse("subs_json"),
            content_class=content_class,
            title_id=title_id,
            probed=row["probe_version"] is not None,
            in_open_duplicate_group=in_open_duplicate_group,
            is_keeper=is_keeper,
        )


    @classmethod
    def from_media_info(
        cls,
        info,
        *,
        content_class: str = "tv",
        title_id: int | None = None,
        in_open_duplicate_group: bool = False,
    ) -> "FileFacts":
        """Build from a fresh ffprobe rather than from the database row.

        The worker re-probes immediately before touching a file, so protection
        and policy are re-checked against what is on disk *now* rather than
        against facts that may be days old. An *arr upgrade that replaced an
        SDR file with an HDR one between planning and encoding is exactly the
        case this exists for.
        """
        from dataclasses import asdict

        return cls(
            file_id=0,
            path=info.path,
            size_bytes=info.size_bytes,
            duration_s=info.duration_s,
            container=info.container,
            v_codec=info.v_codec,
            v_bit_depth=info.v_bit_depth,
            v_width=info.v_width,
            v_height=info.v_height,
            v_bitrate=info.v_bitrate,
            v_fps=info.v_fps,
            hdr_type=info.hdr_type,
            audio=[asdict(track) for track in info.audio],
            subs=[asdict(track) for track in info.subs],
            content_class=content_class,
            title_id=title_id,
            in_open_duplicate_group=in_open_duplicate_group,
        )


@dataclass
class PlannedDecision:
    file_id: int
    action: str
    reason: str
    profile: str | None = None
    est_out_bytes: int | None = None
    est_saved_bytes: int | None = None
    est_cpu_seconds: float | None = None
    priority: float = 0.0
    # Set by the planner when a title has already filled its share of the queue.
    deferred: bool = False
    estimate_basis: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_work(self) -> bool:
        return self.action != SKIP


def _skip(file_id: int, reason: str) -> PlannedDecision:
    return PlannedDecision(file_id=file_id, action=SKIP, reason=reason)


def video_block_reason(facts: FileFacts, policy) -> str | None:
    """Why re-encoding this video would be pointless, or None if it is worth it.

    Kept separate from the protection check because a file can fail this and
    still be a fine remux candidate -- an HEVC film carrying five foreign audio
    tracks has no video work to do and gigabytes of audio to drop.
    """
    if not facts.v_codec:
        return "no readable video stream"
    if facts.v_codec in EFFICIENT_CODECS:
        return f"already {facts.v_codec}; re-encoding would make it bigger"
    if facts.duration_s <= 0:
        return "unknown duration, so nothing can be estimated"

    floor = MIN_ENCODE_BITRATE.get(facts.tier, 1_000_000)
    if facts.v_bitrate and facts.v_bitrate < floor:
        return (
            f"{facts.v_bitrate / 1e6:.1f} Mbps is already below the "
            f"{floor / 1e6:.1f} Mbps floor for {facts.tier}"
        )
    return None


def target_height_for(facts: FileFacts, policy) -> int | None:
    """1080 when a 4K SDR source should be downscaled, else None.

    Only ever reached for SDR: is_protected has already excluded HDR and DV,
    which is the whole reason 4K downscaling is safe to automate here at all.
    """
    if facts.tier == "2160p" and policy.downscale_sdr_4k:
        return 1080
    return None


def decide(facts: FileFacts, config, ladder, estimator) -> PlannedDecision:
    """The plan for one file.

    The estimator is injected rather than imported so policy stays testable
    with a stub, and so a later self-calibrating estimator can replace it
    without any of these rules changing.
    """
    policy, quality = config.policy, config.quality

    if not facts.probed:
        return _skip(facts.file_id, "not probed yet")

    # --- safety, before anything else ---------------------------------------
    if facts.is_protected:
        return _skip(
            facts.file_id,
            f"protected: {facts.hdr_type} is never rewritten -- this box can "
            f"only encode HEVC in 8-bit",
        )

    # --- do not spend CPU on a file that may be about to be deleted ----------
    if facts.in_open_duplicate_group:
        return _skip(
            facts.file_id,
            "in an unresolved duplicate group; settle that first rather than "
            "encoding a copy you may be about to delete",
        )

    if policy.min_source_bytes and facts.size_bytes < policy.min_source_bytes:
        return _skip(
            facts.file_id,
            f"below the {policy.min_source_bytes / 1024**2:.0f} MB minimum "
            f"source size: same per-file cost, far less to reclaim",
        )

    # --- video ---------------------------------------------------------------
    blocked = video_block_reason(facts, policy)
    target_height = None if blocked else target_height_for(facts, policy)
    out_tier = "1080p" if target_height == 1080 else facts.tier

    if not blocked:
        blocked = ladder.unusable_at(out_tier)

    rung = None
    keeper_note = ""
    if not blocked:
        rung = ladder.rung_for(
            facts.content_class, out_tier, prefer_software=facts.is_keeper
        )
        if rung is None:
            blocked = (
                f"no calibrated setting for {facts.content_class} at {out_tier}; "
                f"benchmark some content of that kind"
            )
        elif facts.is_keeper:
            keeper_note = (
                " -- on the x265 keepers list"
                if not rung.is_hardware
                else " -- on the keepers list, but no software rung is "
                     "calibrated for it, so this uses the hardware encoder. Run "
                     "the benchmark with --include-software to get one"
            )

    if not blocked and rung is not None:
        estimate = estimator.encode(facts, rung, target_height, config)
        # Phase 0 measured which decode path is faster (and which works at all)
        # for this encoder. Carry it onto the decision so the worker runs the
        # command that was actually benchmarked.
        estimate.detail["hw_decode"] = ladder.hw_decode.get(rung.encoder, True)
        saved_pct = (
            100.0 * estimate.saved_bytes / facts.size_bytes if facts.size_bytes else 0.0
        )
        if saved_pct < quality.savings_floor_pct:
            blocked = (
                f"predicted saving is only {saved_pct:.0f}%, below the "
                f"{quality.savings_floor_pct:.0f}% floor"
            )
        else:
            return PlannedDecision(
                file_id=facts.file_id,
                action=DOWNSCALE if target_height else ENCODE,
                reason=(
                    f"{facts.v_codec} {facts.tier} -> hevc {out_tier}, "
                    f"saving about {saved_pct:.0f}%{keeper_note}"
                ),
                profile=rung.label,
                est_out_bytes=estimate.out_bytes,
                est_saved_bytes=estimate.saved_bytes,
                est_cpu_seconds=estimate.cpu_seconds,
                estimate_basis=estimate.basis,
                detail=estimate.detail,
            )

    # --- audio-only remux ----------------------------------------------------
    # Nearly free, so it is worth a look even when the video is a lost cause.
    remux = estimator.remux(facts, config)
    if remux.saved_bytes >= policy.audio_remux_min_saving_bytes:
        return PlannedDecision(
            file_id=facts.file_id,
            action=REMUX,
            reason=(
                f"no video work ({blocked}), but dropping "
                f"{remux.detail.get('dropped_tracks', 0)} audio track(s) frees "
                f"{remux.saved_bytes / 1024**3:.1f} GB for almost no CPU"
            ),
            profile="stream copy",
            est_out_bytes=remux.out_bytes,
            est_saved_bytes=remux.saved_bytes,
            est_cpu_seconds=remux.cpu_seconds,
            estimate_basis=remux.basis,
            detail=remux.detail,
        )

    return _skip(facts.file_id, blocked or "nothing worth doing")
