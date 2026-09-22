"""Tests for subtitle cleaning: encoding fallback, ads literal handling, separator."""

import shutil
from pathlib import Path

import pytest

import library.clean_subtitles as clean_subtitles
from library.subtitle_utils import SubtitleUtils


UTF16_SRT = (
    b"\xff\xfe1\x000\x000\x00:\x000\x000\x00:\x000\x000\x00,\x000\x000\x000\x00"
    b"\x00\x00-->\x00 \x000\x000\x00:\x000\x000\x00:\x000\x000\x00,\x000\x000\x000\x00"
    b"\x00\x00H\x00e\x00l\x00l\x00o\x00"
)

ARABIC_CP1256_SRT = (
    b"1\n00:00:00,000 --> 00:00:01,000\n\xc7\xe1\xe3\xd3\xc7\xc1 \xc7\xe1\xe4\xe6\xd1\n"
)


def test_read_file_decodes_utf16(tmp_path):
    path = tmp_path / "utf16.srt"
    path.write_bytes(UTF16_SRT)

    text = clean_subtitles.read_file(path)

    assert "Hello" in text


def test_read_file_decodes_cp1256_arabic(tmp_path):
    path = tmp_path / "arabic.srt"
    path.write_bytes(ARABIC_CP1256_SRT)

    text = clean_subtitles.read_file(path)

    assert "المساء" in text


def test_clean_ads_treats_patterns_as_literals(tmp_path):
    subtitle = tmp_path / "sub.srt"
    subtitle.write_text("hello world\n[world\n**spam\n(+foo\n", encoding="utf-8")
    ads = tmp_path / "ads.txt"
    ads.write_text("[world\n**spam\n(+foo", encoding="utf-8")

    clean_subtitles.clean_ads_regex(subtitle, ads.read_text(encoding="utf-8").splitlines())

    content = subtitle.read_text(encoding="utf-8")
    assert "hello world" in content
    assert "[world" not in content
    assert "**spam" not in content
    assert "(+foo" not in content


def test_clean_ads_with_custom_separator(tmp_path):
    subtitle = tmp_path / "sub.srt"
    subtitle.write_text("Keep me\nRemove this\n", encoding="utf-8")
    ads = tmp_path / "ads.txt"
    ads.write_text("Remove this;Also this", encoding="utf-8")

    clean_subtitles.clean_ads(subtitle, ads_file_path=ads, ads_separator=";")

    content = subtitle.read_text(encoding="utf-8")
    assert "Keep me" in content
    assert "Remove this" not in content
    assert "Also this" not in content


def test_subtitle_utils_clean_strict_threads_separator(tmp_path):
    subtitle = tmp_path / "sub.srt"
    subtitle.write_text("Keep me\nRemove this\n", encoding="utf-8")
    ads = tmp_path / "ads.txt"
    ads.write_text("Remove this;Also this", encoding="utf-8")

    result = SubtitleUtils().clean_subtitles_strict(
        subtitle,
        ads_path=ads,
        ads_separator=";",
    )

    assert result is True
    content = subtitle.read_text(encoding="utf-8")
    assert "Keep me" in content
    assert "Remove this" not in content


def test_clean_ads_preserves_unmatched_lines(tmp_path):
    subtitle = tmp_path / "sub.srt"
    subtitle.write_text("first line\nsecond line\n", encoding="utf-8")
    ads = tmp_path / "ads.txt"
    ads.write_text("third line", encoding="utf-8")

    clean_subtitles.clean_ads_regex(subtitle, ads.read_text(encoding="utf-8").splitlines())

    content = subtitle.read_text(encoding="utf-8")
    assert "first line" in content
    assert "second line" in content


def test_save_file_writes_utf8(tmp_path):
    path = tmp_path / "out.srt"
    clean_subtitles.save_file(path, "النص")

    assert path.read_bytes().startswith(b"\xd8\xa7\xd9\x84\xd9\x86\xd8\xb5")
