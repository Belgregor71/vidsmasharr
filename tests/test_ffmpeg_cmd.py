"""FFmpeg command construction tests.

These lock in details that are easy to break silently and expensive to discover
on a NAS three weeks into a run.
"""

from app.work.ffmpeg_cmd import (
    VideoSpec, build_encode_command, build_vmaf_command, build_video_args, is_hardware,
)

DEVICE = "/dev/dri/renderD128"


def _cmd(spec: VideoSpec, **kwargs) -> list[str]:
    return build_encode_command(
        ffmpeg="ffmpeg", source="/m/in.mkv", dest="/s/out.mkv",
        spec=spec, vaapi_device=DEVICE, **kwargs
    )


def test_hardware_init_flags_come_before_the_input():
    cmd = _cmd(VideoSpec("hevc_vaapi", 24))
    assert cmd.index("-init_hw_device") < cmd.index("-i")
    assert cmd.index("-filter_hw_device") < cmd.index("-i")


def test_codec_flags_come_after_the_input():
    cmd = _cmd(VideoSpec("hevc_vaapi", 24))
    assert cmd.index("-c:v") > cmd.index("-i")


def test_vaapi_uses_integer_qp_under_cqp():
    cmd = _cmd(VideoSpec("hevc_vaapi", 24.7))
    assert "-rc_mode" in cmd and cmd[cmd.index("-rc_mode") + 1] == "CQP"
    # -qp will not accept a float.
    assert cmd[cmd.index("-qp") + 1] == "24"


def test_qsv_uses_global_quality_and_no_lookahead():
    cmd = _cmd(VideoSpec("hevc_qsv", 24))
    assert cmd[cmd.index("-global_quality") + 1] == "24"
    assert cmd[cmd.index("-look_ahead") + 1] == "0"


def test_downscale_inserts_a_hardware_scaler_not_a_software_one():
    cmd = _cmd(VideoSpec("hevc_vaapi", 26, target_height=1080))
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale_vaapi" in vf
    assert "h=1080" in vf
    # -2 keeps the aspect ratio and guarantees an even width.
    assert "w=-2" in vf


def test_software_downscale_uses_a_software_scaler():
    cmd = _cmd(VideoSpec("libx265", 20, target_height=1080))
    assert "scale=-2:1080" in cmd[cmd.index("-vf") + 1]


def test_x265_ten_bit_selects_a_ten_bit_pixel_format():
    cmd = _cmd(VideoSpec("libx265", 20, bit_depth=10))
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p10le"


def test_x265_eight_bit_selects_eight_bit():
    cmd = _cmd(VideoSpec("libx265", 20, bit_depth=8))
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"


def test_thread_cap_applies_to_software_only():
    # Capping threads on a hardware encode does nothing useful; the throttle for
    # hardware is the scheduler, not -threads.
    assert "-threads" in _cmd(VideoSpec("libx265", 20), threads=2)
    assert "-threads" not in _cmd(VideoSpec("hevc_vaapi", 24), threads=2)


def test_chapters_and_metadata_are_preserved():
    cmd = _cmd(VideoSpec("hevc_vaapi", 24))
    assert cmd[cmd.index("-map_metadata") + 1] == "0"
    assert cmd[cmd.index("-map_chapters") + 1] == "0"


def test_audio_is_dropped_only_when_no_audio_args_given():
    assert "-an" in _cmd(VideoSpec("hevc_vaapi", 24))
    custom = _cmd(VideoSpec("hevc_vaapi", 24), audio_args=["-c:a", "copy"])
    assert "-an" not in custom and "copy" in custom


def test_is_hardware_classification():
    assert is_hardware("hevc_vaapi")
    assert is_hardware("hevc_qsv")
    assert not is_hardware("libx265")


def test_vaapi_hw_decode_requests_vaapi_output_format():
    pre, _ = build_video_args(VideoSpec("hevc_vaapi", 24, hw_decode=True), DEVICE)
    assert "-hwaccel" in pre and pre[pre.index("-hwaccel_output_format") + 1] == "vaapi"


def test_vaapi_software_decode_uploads_frames_explicitly():
    _, out = build_video_args(VideoSpec("hevc_vaapi", 24, hw_decode=False), DEVICE)
    vf = out[out.index("-vf") + 1]
    assert "hwupload" in vf and "format=nv12" in vf


def test_vmaf_log_path_stays_a_bare_filename():
    # log_path is parsed twice (filtergraph, then filter args), so a directory
    # separator or a drive colon in it breaks the graph. The runner passes a
    # bare name and sets cwd instead.
    cmd = build_vmaf_command(
        ffmpeg="ffmpeg", distorted="/s/out.mkv", reference="/s/ref.mkv",
        log_path="vmaf_abc.json",
    )
    graph = cmd[cmd.index("-lavfi") + 1]
    assert "log_path=vmaf_abc.json" in graph
    assert "/" not in graph.split("log_path=")[1]


def test_vmaf_upscales_the_distorted_side_when_the_encode_downscaled():
    cmd = build_vmaf_command(
        ffmpeg="ffmpeg", distorted="/s/out.mkv", reference="/s/ref.mkv",
        log_path="v.json", reference_height=2160,
    )
    graph = cmd[cmd.index("-lavfi") + 1]
    # The distorted (first) input is the one scaled back up to reference size.
    assert graph.startswith("[0:v]scale=-2:2160")


def test_vmaf_omits_scaling_when_resolution_is_unchanged():
    cmd = build_vmaf_command(
        ffmpeg="ffmpeg", distorted="/s/out.mkv", reference="/s/ref.mkv",
        log_path="v.json",
    )
    graph = cmd[cmd.index("-lavfi") + 1]
    assert "scale=" not in graph


def test_vmaf_forces_one_frame_rate_on_both_legs():
    # Matroska rounds timestamps to milliseconds, so 1/23.976s alternates
    # 42, 42, 41 -- and an encode re-derives that phase independently of its
    # source. Without a common cadence libvmaf's framesync pairs the wrong
    # frames about three times in twenty-four, and those score near zero
    # however good the encode is. Measured: mean 90.6 / min 0.0 unforced
    # against mean 98.2 / min 93.4 forced, on the same file.
    cmd = build_vmaf_command(
        ffmpeg="ffmpeg", distorted="/s/out.mkv", reference="/s/ref.mkv",
        log_path="v.json", fps=23.976023976023978,
    )
    graph = cmd[cmd.index("-lavfi") + 1]
    dist, ref = graph.split(";")[0], graph.split(";")[1]
    assert "fps=23.976023976023978," in dist
    assert "fps=23.976023976023978," in ref


def test_vmaf_omits_the_frame_rate_filter_when_none_is_known():
    cmd = build_vmaf_command(
        ffmpeg="ffmpeg", distorted="/s/out.mkv", reference="/s/ref.mkv",
        log_path="v.json",
    )
    assert "fps=" not in cmd[cmd.index("-lavfi") + 1]


def test_vmaf_scales_the_distorted_side_before_forcing_the_rate():
    # Order matters: scale is a distorted-only concern, the rate applies to
    # both, and format must be last so libvmaf sees a plain planar frame.
    cmd = build_vmaf_command(
        ffmpeg="ffmpeg", distorted="/s/out.mkv", reference="/s/ref.mkv",
        log_path="v.json", reference_height=1080, fps="24000/1001",
    )
    dist = cmd[cmd.index("-lavfi") + 1].split(";")[0]
    assert dist.index("scale=") < dist.index("fps=") < dist.index("format=")
