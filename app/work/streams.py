"""Which streams survive into the output, and in what form.

The audio *selection* is imported from the planner rather than re-derived here.
If the plan says a file saves 4GB by dropping three tracks and the worker then
keeps a different set, the estimate that ranked the job was a fiction and
Phase 4 would be calibrating against noise. One rule, one place.

Everything is mapped by absolute ffprobe stream index. Relative specifiers like
`0:a:1` mean "the second audio stream" and quietly shift when a file has
attached cover art or a data stream in the middle, which plenty do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.plan.estimate import select_audio
from app.plan.rules import FileFacts
from app.scan.probe import MediaInfo


@dataclass
class StreamPlan:
    """The audio and subtitle half of an ffmpeg command."""

    args: list[str] = field(default_factory=list)
    kept_audio: list[int] = field(default_factory=list)
    dropped_audio: list[int] = field(default_factory=list)
    kept_subs: list[int] = field(default_factory=list)
    dropped_subs: list[int] = field(default_factory=list)
    audio_transcoded: bool = False
    kept_attachments: bool = False
    note: str = ""

    @property
    def summary(self) -> str:
        return (
            f"audio: keep {len(self.kept_audio)}, drop {len(self.dropped_audio)}"
            f"{' (transcoded)' if self.audio_transcoded else ' (copied)'}; "
            f"subs: keep {len(self.kept_subs)}, drop {len(self.dropped_subs)}"
            f"{'; fonts kept' if self.kept_attachments else ''}"
        )


def choose_subtitles(info: MediaInfo, audio_cfg) -> tuple[list[int], list[int]]:
    """Keep the wanted languages, plus any forced track whatever its language.

    A forced track carries the subtitles for the foreign-language scenes inside
    an English film. Dropping it because it is tagged `spa` would make those
    scenes unwatchable, which is a far more visible regression than the handful
    of megabytes it costs to keep.
    """
    wanted = {lang.lower() for lang in audio_cfg.keep_languages}
    keep, drop = [], []
    for track in info.subs:
        forced = track.is_forced and audio_cfg.keep_forced_subs
        if forced or (track.language or "und").lower() in wanted:
            keep.append(track.index)
        else:
            drop.append(track.index)
    return keep, drop


def build_stream_plan(info: MediaInfo, config, *, copy_audio: bool = False) -> StreamPlan:
    """Audio + subtitle arguments for an encode or a remux.

    `copy_audio` forces a stream copy even where policy would transcode. Used
    by nothing yet; it exists so a future "container change only" action cannot
    accidentally re-encode audio.
    """
    audio_cfg = config.audio
    facts = FileFacts.from_media_info(info)
    selection = select_audio(facts, audio_cfg)

    keep_indexes = [int(track["index"]) for track in selection["keep"]]
    drop_indexes = [int(track["index"]) for track in selection["drop"]]
    transcode = selection["transcode"] and not copy_audio

    keep_subs, drop_subs = choose_subtitles(info, audio_cfg)

    args: list[str] = []
    for index in keep_indexes:
        args += ["-map", f"0:{index}"]
    for index in keep_subs:
        args += ["-map", f"0:{index}"]

    # Fonts ride along with the subtitles that need them. An ASS track carries
    # the typesetting for signs and songs by *reference* to fonts attached to
    # the container, so dropping the attachments leaves the track rendering in
    # whatever the player falls back to -- on an anime rip that styling is most
    # of the reason the track was worth keeping. They cost a few MB against a
    # feature film. `?` makes the map optional: most files carry no attachments
    # and ffmpeg must not fail on them.
    if keep_subs:
        args += ["-map", "0:t?"]

    if not keep_indexes:
        # Should be unreachable -- select_audio keeps everything rather than
        # returning nothing -- but a file with genuinely no audio exists.
        args += ["-an"]
    elif transcode:
        source_channels = max(
            (int(track.get("channels") or 0) for track in selection["keep"]), default=0
        )
        args += [
            "-c:a", audio_cfg.target_codec,
            "-b:a", str(audio_cfg.target_bitrate),
        ]
        # Never upmix. A stereo source pushed to 5.1 is bigger and no better.
        if source_channels:
            args += ["-ac", str(min(source_channels, audio_cfg.target_channels))]
    else:
        args += ["-c:a", "copy"]

    if keep_subs:
        # Always copy: re-encoding a subtitle is never what we want, and image
        # subtitles (PGS) cannot be re-encoded at all.
        args += ["-c:s", "copy"]
    else:
        args += ["-sn"]

    # The surviving audio track has to be the default one, or a player picks
    # nothing and the file appears to have no sound.
    if keep_indexes:
        args += ["-disposition:a:0", "default"]

    return StreamPlan(
        args=args,
        kept_audio=keep_indexes,
        dropped_audio=drop_indexes,
        kept_subs=keep_subs,
        dropped_subs=drop_subs,
        audio_transcoded=transcode,
        kept_attachments=bool(keep_subs),
        note=selection["note"],
    )
