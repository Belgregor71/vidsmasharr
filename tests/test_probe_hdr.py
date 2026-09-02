"""HDR detection tests.

These matter more than any other test in the project: a false negative here
sends an HDR file through an 8-bit hardware encoder and destroys the grade
permanently, with the original already deleted. Every case that should be
protected is asserted explicitly.
"""

import pytest

from app.scan.probe import detect_hdr, parse_probe_json

SDR_1080P = {
    "codec_name": "h264",
    "pix_fmt": "yuv420p",
    "bits_per_raw_sample": "8",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "width": 1920,
    "height": 1080,
}


def test_plain_sdr_h264_is_the_only_encodable_case():
    assert detect_hdr(SDR_1080P) == "sdr"


def test_hdr10_pq_transfer_is_protected():
    stream = {**SDR_1080P, "color_transfer": "smpte2084",
              "color_primaries": "bt2020", "bits_per_raw_sample": "10"}
    assert detect_hdr(stream) == "hdr10"


def test_hlg_broadcast_hdr_is_protected():
    stream = {**SDR_1080P, "color_transfer": "arib-std-b67",
              "color_primaries": "bt2020"}
    assert detect_hdr(stream) == "hlg"


def test_dolby_vision_side_data_is_protected():
    stream = {**SDR_1080P, "side_data_list": [
        {"side_data_type": "DOVI configuration record", "dv_profile": 8}
    ]}
    assert detect_hdr(stream) == "dolbyvision"


def test_hdr10_plus_dynamic_metadata_is_protected():
    stream = {**SDR_1080P, "side_data_list": [
        {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}
    ]}
    assert detect_hdr(stream) == "hdr10plus"


def test_dolby_vision_wins_over_a_bt709_tag():
    # DV files sometimes carry misleading colour tags; side data is authoritative.
    stream = {**SDR_1080P, "color_transfer": "bt709",
              "side_data_list": [{"side_data_type": "DOVI configuration record"}]}
    assert detect_hdr(stream) == "dolbyvision"


def test_bt2020_primaries_alone_is_protected():
    # Stripped or mislabelled HDR. Not proof, but we do not gamble.
    stream = {**SDR_1080P, "color_transfer": "bt709", "color_primaries": "bt2020"}
    assert detect_hdr(stream) == "hdr10"


def test_ten_bit_without_hdr_signalling_is_protected():
    stream = {**SDR_1080P, "pix_fmt": "yuv420p10le", "bits_per_raw_sample": "10"}
    assert detect_hdr(stream) == "unknown-10bit"


def test_twelve_bit_is_protected():
    stream = {**SDR_1080P, "pix_fmt": "yuv420p12le", "bits_per_raw_sample": None}
    assert detect_hdr(stream) == "unknown-10bit"


def test_undeterminable_depth_is_protected_not_assumed_sdr():
    stream = {"codec_name": "h264", "width": 1920, "height": 1080}
    assert detect_hdr(stream) == "unknown"


def test_bit_depth_falls_back_to_pix_fmt_when_tag_missing():
    stream = {**SDR_1080P, "bits_per_raw_sample": None, "pix_fmt": "yuv420p10le"}
    assert detect_hdr(stream) == "unknown-10bit"


def test_only_sdr_is_unprotected_via_media_info():
    for hdr_stream, expected_protected in [
        (SDR_1080P, False),
        ({**SDR_1080P, "color_transfer": "smpte2084"}, True),
        ({**SDR_1080P, "pix_fmt": "yuv420p10le"}, True),
        ({"codec_name": "h264"}, True),
    ]:
        info = parse_probe_json(
            "/x.mkv", 1000,
            {"format": {"duration": "60", "bit_rate": "5000000",
                        "format_name": "matroska,webm"},
             "streams": [{**hdr_stream, "codec_type": "video"}]},
        )
        assert info.is_protected_hdr is expected_protected, info.hdr_type


def test_attached_cover_art_is_not_mistaken_for_the_video_stream():
    data = {
        "format": {"duration": "3600", "bit_rate": "5000000",
                   "format_name": "matroska,webm"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 600,
             "height": 900, "disposition": {"attached_pic": 1}},
            {**SDR_1080P, "codec_type": "video", "disposition": {}},
        ],
    }
    info = parse_probe_json("/x.mkv", 1000, data)
    assert info.v_codec == "h264"
    assert info.v_width == 1920


def test_file_with_no_video_stream_is_unknown_not_sdr():
    data = {"format": {"duration": "60", "format_name": "matroska,webm"},
            "streams": [{"codec_type": "audio", "codec_name": "aac"}]}
    info = parse_probe_json("/x.mkv", 1000, data)
    assert info.hdr_type == "unknown"
    assert info.is_protected_hdr is True


def _with_video(video_stream, duration="8521.568000"):
    return parse_probe_json(
        "/x.mkv", 1000,
        {"format": {"duration": duration, "bit_rate": "5000000",
                    "format_name": "matroska,webm"},
         "streams": [{**SDR_1080P, "codec_type": "video", **video_stream}]},
    )


def test_video_duration_is_read_from_a_language_suffixed_matroska_tag():
    """mkvmerge writes DURATION-eng, not DURATION. Reading only the bare tag
    misses it on most Bluray rips -- which is how six good remuxes came to be
    measured against their container length instead of their picture."""
    info = _with_video({"tags": {"language": "eng",
                                 "DURATION-eng": "02:17:32.244000000"}})
    assert info.v_duration_s == pytest.approx(8252.244)
    assert info.duration_s == pytest.approx(8521.568)


def test_video_duration_is_read_from_a_bare_duration_tag():
    info = _with_video({"tags": {"DURATION": "01:44:59.460000000"}})
    assert info.v_duration_s == pytest.approx(6299.46)


def test_video_duration_prefers_the_stream_field_when_the_container_has_one():
    info = _with_video({"duration": "6299.46",
                        "tags": {"DURATION-eng": "99:00:00.000000000"}})
    assert info.v_duration_s == pytest.approx(6299.46)


def test_video_duration_is_none_when_nothing_records_it():
    assert _with_video({"tags": {"language": "eng"}}).v_duration_s is None
