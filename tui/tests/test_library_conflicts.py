"""Tests for legacy library-layer subtitle conflict checks (M11)."""

import io
import zipfile
from pathlib import Path

import pytest
import requests

from library.OpenSubtitles import OpenSubtitles
from library.SubDL import SubDL
from library.SubSource import SubSource
from library.subtitle_utils import report_existing_subtitle


def _make_zip(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


def _fake_console():
    """Return a minimal console stand-in that records printed messages."""
    console = type("Console", (), {})()
    console.messages = []
    console.print = lambda msg: console.messages.append(msg)
    return console


def test_report_existing_subtitle_returns_true_when_file_exists(tmp_path):
    existing = tmp_path / "Movie.ar.srt"
    existing.write_text("existing", encoding="utf-8")
    console = _fake_console()

    assert report_existing_subtitle(existing, console) is True
    assert any("Subtitle already exists" in msg for msg in console.messages)


def test_report_existing_subtitle_returns_false_when_file_missing(tmp_path):
    missing = tmp_path / "Movie.ar.srt"
    console = _fake_console()

    assert report_existing_subtitle(missing, console) is False
    assert not console.messages


def test_opensubtitles_process_media_file_skips_existing_subtitle(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(OpenSubtitles, "login", lambda self: "token")
    client = OpenSubtitles("u", "p", "k", "ua", output_directory=None)
    video = tmp_path / "Movie.mkv"
    video.write_text("x")
    existing = tmp_path / "Movie.ar.srt"
    existing.write_text("existing", encoding="utf-8")

    save_calls = []

    def fake_save(*args, **kwargs):
        save_calls.append(True)
        return True

    monkeypatch.setattr(client, "save_subtitle", fake_save)

    result = client.process_media_file(str(video), "ar")

    assert result is False
    assert not save_calls
    assert existing.read_text(encoding="utf-8") == "existing"
    captured = capsys.readouterr()
    assert "Subtitle already exists" in captured.out


def test_subdl_single_file_skips_existing_subtitle(tmp_path, monkeypatch):
    client = SubDL("key", output_directory=None)
    video = tmp_path / "Movie.mkv"
    video.write_text("x")
    existing = tmp_path / "Movie.ar.srt"
    existing.write_text("existing", encoding="utf-8")

    get_calls = []

    def fake_get(*args, **kwargs):
        get_calls.append(True)

        class Resp:
            content = b"subtitle"
            text = "subtitle"

            @staticmethod
            def raise_for_status():
                pass

        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)

    result = client._download_single_file(
        "http://example.com/sub.srt", "srt", video, "ar"
    )

    assert result is None
    assert existing.read_text(encoding="utf-8") == "existing"


def test_subdl_single_file_downloads_when_no_conflict(tmp_path, monkeypatch):
    client = SubDL("key", output_directory=None)
    video = tmp_path / "Movie.mkv"
    video.write_text("x")

    def fake_get(*args, **kwargs):
        class Resp:
            content = b"subtitle"
            text = "subtitle"

            @staticmethod
            def raise_for_status():
                pass

        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)

    result = client._download_single_file(
        "http://example.com/sub.srt", "srt", video, "ar"
    )

    assert result is not None
    assert result.exists()
    assert result.read_text(encoding="utf-8") == "subtitle"


def test_subdl_zip_skips_existing_subtitle(tmp_path, monkeypatch):
    client = SubDL("key", output_directory=None)
    video = tmp_path / "Movie.mkv"
    video.write_text("x")
    existing = tmp_path / "Movie.ar.srt"
    existing.write_text("existing", encoding="utf-8")
    zip_bytes = _make_zip("Movie.srt", b"subtitle")

    def fake_get(*args, **kwargs):
        class Resp:
            @staticmethod
            def raise_for_status():
                pass

            @staticmethod
            def iter_content(chunk_size=8192):
                for i in range(0, len(zip_bytes), chunk_size):
                    yield zip_bytes[i : i + chunk_size]

        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)

    result = client._download_zip(
        "http://example.com/sub.zip", video, "ar", None, None, True
    )

    assert result is None
    assert existing.read_text(encoding="utf-8") == "existing"


def test_subsource_archive_skips_existing_subtitle(tmp_path, monkeypatch):
    client = SubSource("key", output_directory=None)
    video = tmp_path / "Movie.mkv"
    video.write_text("x")
    existing = tmp_path / "Movie.ar.srt"
    existing.write_text("existing", encoding="utf-8")
    zip_bytes = _make_zip("Movie.srt", b"subtitle")

    def fake_get_raw(self, url):
        class Resp:
            @staticmethod
            def iter_content(chunk_size=8192):
                for i in range(0, len(zip_bytes), chunk_size):
                    yield zip_bytes[i : i + chunk_size]

        return Resp()

    monkeypatch.setattr(SubSource, "_get_raw", fake_get_raw)

    result = client._download_archive(
        {"id": 123}, video, "ar", None, None, True
    )

    assert result is None
    assert existing.read_text(encoding="utf-8") == "existing"


def test_subsource_archive_downloads_when_no_conflict(tmp_path, monkeypatch):
    client = SubSource("key", output_directory=None)
    video = tmp_path / "Movie.mkv"
    video.write_text("x")
    zip_bytes = _make_zip("Movie.srt", b"subtitle")

    def fake_get_raw(self, url):
        class Resp:
            @staticmethod
            def iter_content(chunk_size=8192):
                for i in range(0, len(zip_bytes), chunk_size):
                    yield zip_bytes[i : i + chunk_size]

        return Resp()

    monkeypatch.setattr(SubSource, "_get_raw", fake_get_raw)

    result = client._download_archive(
        {"id": 123}, video, "ar", None, None, True
    )

    assert result is not None
    assert result.exists()
    assert result.read_text(encoding="utf-8") == "subtitle"
