"""Tests for the post-download subtitle verifier."""

from pathlib import Path

from library.subtitle_verifier import SubtitleVerifier


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_empty_subtitle_fails(tmp_path):
    sub = tmp_path / "Movie.ar.srt"
    _write(sub, "")
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert not result.passed
    assert "empty" in result.reason.lower()


def test_whitespace_only_subtitle_fails(tmp_path):
    sub = tmp_path / "Movie.ar.srt"
    _write(sub, "   \n   \n")
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert not result.passed
    assert "empty" in result.reason.lower()


def test_malformed_srt_fails(tmp_path):
    sub = tmp_path / "Movie.ar.srt"
    _write(sub, "this is not a subtitle\n")
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert not result.passed
    assert "format" in result.reason.lower()


def test_valid_srt_passes(tmp_path):
    sub = tmp_path / "Movie.ar.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:03,000\nالمساء الخير\n"
        "2\n00:00:04,000 --> 00:00:06,000\nكيف حالك\n",
    )
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert result.passed


def test_valid_ass_passes(tmp_path):
    sub = tmp_path / "Movie.ar.ass"
    _write(sub, "[Script Info]\nTitle: Test\n\n[V4+ Styles]\nالمساء الخير\n")
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert result.passed


def test_valid_ssa_passes(tmp_path):
    sub = tmp_path / "Movie.ar.ssa"
    _write(sub, "[Script Info]\nTitle: Test\n\n[V4 Styles]\nالمساء الخير\n")
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert result.passed


def test_valid_vtt_passes(tmp_path):
    sub = tmp_path / "Movie.ar.vtt"
    _write(sub, "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nالمساء الخير\n")
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert result.passed


def test_english_subtitle_for_arabic_request_fails(tmp_path):
    sub = tmp_path / "Movie.ar.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:03,000\nHello world\n",
    )
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert not result.passed
    assert "language" in result.reason.lower()


def test_arabic_subtitle_for_english_request_fails(tmp_path):
    sub = tmp_path / "Movie.en.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:03,000\nالمساء الخير\n",
    )
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "en")
    assert not result.passed
    assert "language" in result.reason.lower()


def test_unknown_language_skips_language_check(tmp_path):
    sub = tmp_path / "Movie.xx.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:03,000\nHello world\n",
    )
    verifier = SubtitleVerifier()
    result = verifier.verify(sub, tmp_path / "Movie.mkv", None)
    assert result.passed


def test_coverage_too_short_fails(tmp_path, monkeypatch):
    sub = tmp_path / "Movie.ar.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:03,000\nالمساء الخير\n",
    )
    verifier = SubtitleVerifier()
    monkeypatch.setattr(verifier, "_video_duration", lambda media_path: 600.0)
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert not result.passed
    assert "duration" in result.reason.lower()


def test_coverage_valid_passes(tmp_path, monkeypatch):
    sub = tmp_path / "Movie.ar.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:05,000\nالمساء الخير\n"
        "2\n00:09:55,000 --> 00:10:00,000\nالخيطام\n",
    )
    verifier = SubtitleVerifier()
    monkeypatch.setattr(verifier, "_video_duration", lambda media_path: 600.0)
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert result.passed


def test_coverage_overrun_fails(tmp_path, monkeypatch):
    sub = tmp_path / "Movie.ar.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:20:00,000\nالمساء الخير\n",
    )
    verifier = SubtitleVerifier()
    monkeypatch.setattr(verifier, "_video_duration", lambda media_path: 600.0)
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert not result.passed
    assert "duration" in result.reason.lower()


def test_ffprobe_missing_skips_duration_check(tmp_path, monkeypatch):
    sub = tmp_path / "Movie.ar.srt"
    _write(
        sub,
        "1\n00:00:01,000 --> 00:00:03,000\nالمساء الخير\n",
    )
    verifier = SubtitleVerifier()
    monkeypatch.setattr(verifier, "_video_duration", lambda media_path: None)
    result = verifier.verify(sub, tmp_path / "Movie.mkv", "ar")
    assert result.passed
