"""Phase 0, step 1: what can this box actually do?

Spec sheets say Apollo Lake supports HEVC 8-bit encode. What matters is whether
*this* DSM kernel, *this* container's render-group permissions and *this* ffmpeg
build agree. So every capability is confirmed by really encoding a synthetic
clip, not by reading a feature list.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

from app.work.ffmpeg_cmd import VideoSpec, build_video_args

# Encoders worth testing on Apollo Lake, in the order we would prefer them.
CANDIDATE_ENCODERS = ["hevc_vaapi", "hevc_qsv", "h264_vaapi", "libx265", "libx264"]


@dataclass
class EncoderCheck:
    encoder: str
    listed: bool = False        # ffmpeg -encoders knows about it
    works: bool = False         # a real 1-second encode succeeded
    error: str | None = None


@dataclass
class Capabilities:
    ffmpeg: str = ""
    ffmpeg_version: str = ""
    ffprobe_version: str = ""
    # Scoring runs on a separate binary: jellyfin-ffmpeg has VAAPI but no
    # libvmaf, so one binary cannot do both jobs.
    ffmpeg_vmaf: str = ""
    ffmpeg_vmaf_version: str = ""
    has_libvmaf: bool = False
    render_nodes: list[str] = field(default_factory=list)
    render_node_readable: bool = False
    render_node_gid: int | None = None
    vainfo: str | None = None
    vainfo_hevc_encode: bool = False
    encoders: list[EncoderCheck] = field(default_factory=list)
    cpu_model: str = ""
    cpu_count: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def preferred_hw_encoder(self) -> str | None:
        for name in ("hevc_vaapi", "hevc_qsv"):
            check = next((e for e in self.encoders if e.encoder == name), None)
            if check and check.works:
                return name
        return None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["preferred_hw_encoder"] = self.preferred_hw_encoder
        return data


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _tool_version(tool: str) -> str:
    try:
        result = _run([tool, "-version"], timeout=20)
        return result.stdout.splitlines()[0] if result.stdout else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _cpu_info() -> tuple[str, int]:
    model = ""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
        if match:
            model = match.group(1).strip()
    except OSError:
        pass
    return model, os.cpu_count() or 0


def _find_render_nodes() -> list[str]:
    dri = Path("/dev/dri")
    if not dri.is_dir():
        return []
    return sorted(str(p) for p in dri.glob("renderD*"))


def _list_encoders(ffmpeg: str) -> set[str]:
    result = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    found = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line)
        if match:
            found.add(match.group(1))
    return found


def _has_filter(ffmpeg: str, name: str) -> bool:
    result = _run([ffmpeg, "-hide_banner", "-filters"], timeout=30)
    return any(re.search(rf"\s{re.escape(name)}\s", line) for line in result.stdout.splitlines())


def _probe_vainfo(device: str) -> tuple[str | None, bool]:
    if not shutil.which("vainfo"):
        return None, False
    result = _run(["vainfo", "--display", "drm", "--device", device], timeout=30)
    text = (result.stdout or "") + (result.stderr or "")
    if not text.strip():
        return None, False
    hevc_encode = bool(re.search(r"VAProfileHEVCMain\s*:\s*VAEntrypointEncSlice", text))
    return text.strip(), hevc_encode


def _test_encode(ffmpeg: str, encoder: str, device: str) -> tuple[bool, str | None]:
    """Encode one second of synthetic 720p video. Cheap, and definitive."""
    spec = VideoSpec(encoder=encoder, quality=28, hw_decode=False)
    _, video_out = build_video_args(spec, device)

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "probe.mkv"
        pre: list[str] = []
        if encoder.endswith("_vaapi"):
            pre = ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"]
        elif encoder.endswith("_qsv"):
            pre = ["-init_hw_device", f"qsv=hw:{device}", "-filter_hw_device", "hw"]

        cmd = (
            [ffmpeg, "-hide_banner", "-nostdin", "-y"]
            + pre
            + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=1"]
            + video_out
            + ["-an", str(dest)]
        )
        try:
            result = _run(cmd, timeout=120)
        except subprocess.SubprocessError as exc:
            return False, str(exc)

        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()
            return False, " | ".join(tail[-3:])[:400]
        if not dest.exists() or dest.stat().st_size == 0:
            return False, "encoder reported success but produced no output"
    return True, None


def detect(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
           device: str | None = None, ffmpeg_vmaf: str | None = None) -> Capabilities:
    caps = Capabilities(ffmpeg=ffmpeg)
    caps.ffmpeg_version = _tool_version(ffmpeg)
    caps.ffprobe_version = _tool_version(ffprobe)
    caps.ffmpeg_vmaf = ffmpeg_vmaf or os.environ.get("VIDSMASHARR_FFMPEG_VMAF") or ffmpeg
    caps.cpu_model, caps.cpu_count = _cpu_info()

    if not caps.ffmpeg_version:
        caps.notes.append(f"ffmpeg not runnable at {ffmpeg!r} -- nothing else can be checked")
        return caps

    caps.ffmpeg_vmaf_version = _tool_version(caps.ffmpeg_vmaf)
    caps.has_libvmaf = _has_filter(caps.ffmpeg_vmaf, "libvmaf")
    if not caps.has_libvmaf:
        same = caps.ffmpeg_vmaf == ffmpeg
        caps.notes.append(
            f"No libvmaf in {caps.ffmpeg_vmaf}: the quality ladder cannot be "
            "calibrated and encodes cannot be quality-verified. "
            + (
                "jellyfin-ffmpeg is built for streaming and omits libvmaf, so the "
                "image carries a second static build at /opt/ffmpeg-vmaf/bin/ffmpeg "
                "purely for scoring -- it looks like that download failed at build "
                "time. Rebuild with --no-cache."
                if same else
                "Check that the scoring binary exists and was built with libvmaf."
            )
        )

    caps.render_nodes = _find_render_nodes()
    device = device or (caps.render_nodes[0] if caps.render_nodes else "/dev/dri/renderD128")

    if not caps.render_nodes:
        caps.notes.append(
            "No /dev/dri render node visible. Pass --device /dev/dri:/dev/dri to the "
            "container; hardware encoding is impossible without it."
        )
    else:
        try:
            stat = os.stat(device)
            caps.render_node_gid = stat.st_gid
            caps.render_node_readable = os.access(device, os.R_OK | os.W_OK)
        except OSError as exc:
            caps.notes.append(f"cannot stat {device}: {exc}")
        if not caps.render_node_readable:
            caps.notes.append(
                f"{device} exists but is not writable by uid "
                f"{getattr(os, 'getuid', lambda: '?')()}. "
                f"Add the render group (gid {caps.render_node_gid}) via group_add."
            )
        caps.vainfo, caps.vainfo_hevc_encode = _probe_vainfo(device)

    listed = _list_encoders(ffmpeg)
    for name in CANDIDATE_ENCODERS:
        check = EncoderCheck(encoder=name, listed=name in listed)
        if check.listed:
            check.works, check.error = _test_encode(ffmpeg, name, device)
        else:
            check.error = "not present in this ffmpeg build"
        caps.encoders.append(check)

    if caps.preferred_hw_encoder is None:
        caps.notes.append(
            "No working hardware HEVC encoder. Software x265 on a J3455 is roughly "
            "3-6 fps at 1080p -- re-encoding the library is not viable this way. "
            "Fix hardware access before continuing."
        )
    return caps


def render_text(caps: Capabilities) -> str:
    lines = ["=== vidsmasharr capability report ===", ""]
    lines.append(f"CPU            : {caps.cpu_model or 'unknown'} ({caps.cpu_count} threads)")
    lines.append(f"ffmpeg (encode): {caps.ffmpeg_version or 'NOT FOUND'}")
    lines.append(f"ffmpeg (score) : {caps.ffmpeg_vmaf_version or 'NOT FOUND'}")
    lines.append(f"libvmaf filter : {'yes' if caps.has_libvmaf else 'NO'}")
    lines.append(f"render nodes   : {', '.join(caps.render_nodes) or 'NONE'}")
    lines.append(
        f"node access    : {'read/write' if caps.render_node_readable else 'NOT ACCESSIBLE'}"
        + (f" (gid {caps.render_node_gid})" if caps.render_node_gid is not None else "")
    )
    lines.append(f"vainfo HEVC enc: {'yes' if caps.vainfo_hevc_encode else 'no/unknown'}")
    lines.append("")
    lines.append("encoders (verified by real 1s encode):")
    for check in caps.encoders:
        if check.works:
            status = "WORKS"
        elif check.listed:
            status = "FAILED"
        else:
            status = "absent"
        lines.append(f"  {check.encoder:<12} {status}")
        if check.error and not check.works:
            lines.append(f"      -> {check.error}")
    lines.append("")
    lines.append(f"preferred hardware encoder: {caps.preferred_hw_encoder or 'NONE'}")
    if caps.notes:
        lines.append("")
        lines.append("notes:")
        for note in caps.notes:
            lines.append(f"  ! {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Probe encoding capabilities")
    parser.add_argument("--ffmpeg", default=os.environ.get("VIDSMASHARR_FFMPEG", "ffmpeg"))
    parser.add_argument("--ffprobe", default=os.environ.get("VIDSMASHARR_FFPROBE", "ffprobe"))
    parser.add_argument("--device", default=None)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    caps = detect(args.ffmpeg, args.ffprobe, args.device)
    print(json.dumps(caps.to_dict(), indent=2) if args.json else render_text(caps))
    return 0 if caps.preferred_hw_encoder else 1


if __name__ == "__main__":
    raise SystemExit(main())
