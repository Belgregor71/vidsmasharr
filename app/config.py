"""Configuration: YAML file in /config, overridable by VIDSMASHARR_* env vars.

Defaults are deliberately conservative. Anything that can delete data or spend
hours of CPU starts in its safe position and has to be turned on explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

GB = 1024**3

# ---------------------------------------------------------------- sub-sections


class PlexConfig(BaseModel):
    enabled: bool = False
    url: str = "http://localhost:32400"
    token: str = ""
    # Directory holding com.plexapp.plugins.library.db. Mounted read-only; we
    # always copy before opening (see identity/plex.py) rather than touching the
    # live file while the server is running.
    db_dir: Path | None = None
    # Path prefix rewriting: Plex's view of a file vs ours inside the container.
    path_map: dict[str, str] = Field(default_factory=dict)


class ArrConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    path_map: dict[str, str] = Field(default_factory=dict)


class ScheduleConfig(BaseModel):
    # Full-speed window. Local time, HH:MM. Wraps past midnight.
    night_start: str = "23:00"
    night_end: str = "07:00"
    night_threads: int = 4
    # Outside the window we keep working but stay out of the way.
    day_enabled: bool = True
    day_threads: int = 1
    day_nice: int = 15
    # Suspend encoding entirely while someone is actually watching, so we do not
    # contend for /dev/dri and the disks with a live Plex stream.
    pause_when_streaming: bool = True
    plex_poll_seconds: int = 30


class QualityConfig(BaseModel):
    movie_vmaf: float = 95.0
    tv_vmaf: float = 92.0
    # Never spend CPU for a rounding error: skip anything we predict will shrink
    # by less than this.
    savings_floor_pct: float = 20.0
    # Post-encode verification.
    vmaf_sample_count: int = 3
    vmaf_sample_seconds: int = 20
    vmaf_fail_margin: float = 3.0  # allowed shortfall below the target


class EncoderConfig(BaseModel):
    # "auto" resolves from bench results; explicit values override.
    preferred: Literal["auto", "vaapi", "qsv", "x265"] = "auto"
    vaapi_device: Path = Path("/dev/dri/renderD128")
    x265_preset: str = "slow"
    # Software x265 is ~3-6 fps on a J3455. Only files explicitly listed here
    # ever get it, and only in the night window.
    keepers_file: Path | None = None


class AudioConfig(BaseModel):
    keep_languages: list[str] = Field(default_factory=lambda: ["eng", "und"])
    # Copy through if the source track is already one of these and small enough.
    passthrough_codecs: list[str] = Field(
        default_factory=lambda: ["eac3", "ac3", "aac"]
    )
    passthrough_max_bitrate: int = 640_000
    target_codec: str = "eac3"
    target_bitrate: str = "640k"
    target_channels: int = 6
    keep_forced_subs: bool = True


class SafetyConfig(BaseModel):
    # Halt before starting a job if free space would drop below this.
    min_free_bytes: int = 250 * GB
    # Job needs room for the output plus headroom while it works.
    scratch_headroom_factor: float = 1.5
    # Master switch. While False the worker encodes and verifies but leaves the
    # original in place, so a first batch can be eyeballed on the TVs.
    delete_original_on_success: bool = False
    # Absolute ceiling on damage from a runaway loop.
    max_deletes_per_run: int = 50
    dry_run: bool = True


class PolicyConfig(BaseModel):
    # Target video bitrates (bits/sec) for HEVC output. Anything already at or
    # below its target is not worth re-encoding.
    target_bitrate_1080p: int = 3_500_000
    target_bitrate_720p: int = 1_800_000
    target_bitrate_sd: int = 1_000_000
    # SDR 4K is downscaled to 1080p; HDR 4K is never touched (8-bit hardware
    # encode only -- see rules.py).
    downscale_sdr_4k: bool = True
    # Don't let one big show monopolise the queue for weeks.
    max_queued_per_title: int = 25
    # Re-muxing purely to drop audio tracks is nearly free; do it when it buys
    # at least this much.
    audio_remux_min_saving_bytes: int = 300 * 1024 * 1024


# ---------------------------------------------------------------- root


class Config(BaseModel):
    libraries: list[Path] = Field(default_factory=list)
    scratch_dir: Path = Path("/scratch")
    config_dir: Path = Path("/config")

    plex: PlexConfig = Field(default_factory=PlexConfig)
    sonarr: ArrConfig = Field(default_factory=ArrConfig)
    radarr: ArrConfig = Field(default_factory=ArrConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    encoder: EncoderConfig = Field(default_factory=EncoderConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    web_host: str = "0.0.0.0"
    web_port: int = 8330
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.config_dir / "vidsmasharr.db"

    @property
    def profiles_path(self) -> Path:
        return self.config_dir / "profiles.yaml"

    # Video extensions we consider part of a media library.
    media_extensions: set[str] = Field(
        default_factory=lambda: {
            ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv",
            ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".divx",
        }
    )


def _env_overrides() -> dict:
    """VIDSMASHARR_PLEX__TOKEN=abc -> {"plex": {"token": "abc"}}."""
    out: dict = {}
    prefix = "VIDSMASHARR_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        cursor = out
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = value
    return out


def _deep_merge(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path | None = None) -> Config:
    config_dir = Path(os.environ.get("VIDSMASHARR_CONFIG_DIR", "/config"))
    path = path or config_dir / "config.yaml"

    data: dict = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("config_dir", str(config_dir))
    _deep_merge(data, _env_overrides())
    return Config.model_validate(data)
