"""Tests for media hashing edge cases."""

from library.subtitle_utils import SubtitleUtils


def test_hashfile_returns_none_for_small_file(tmp_path):
    tiny = tmp_path / "tiny.mkv"
    tiny.write_bytes(b"too small")

    result = SubtitleUtils().hashFile(tiny)

    assert result is None


def test_hashfile_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "missing.mkv"

    result = SubtitleUtils().hashFile(missing)

    assert result is None


def test_hashfile_returns_hex_string_for_valid_file(tmp_path):
    # The OpenSubtitles hash needs at least 128 KiB; create a file of exactly
    # that size filled with deterministic bytes.
    valid = tmp_path / "valid.mkv"
    valid.write_bytes(b"\x00" * (128 * 1024))

    result = SubtitleUtils().hashFile(valid)

    assert isinstance(result, str)
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_hashfile_never_returns_sentinel_strings(tmp_path):
    tiny = tmp_path / "tiny.mkv"
    tiny.write_bytes(b"x")

    result = SubtitleUtils().hashFile(tiny)

    assert result not in ("SizeError", "IOError")
    assert not isinstance(result, str) or not result.endswith("Error")
