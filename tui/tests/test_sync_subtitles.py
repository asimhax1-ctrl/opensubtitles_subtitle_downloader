from pathlib import Path
from types import SimpleNamespace

from library import sync_subtitles


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_sync_subs_audio_streams_combined_ffsubsync_output(monkeypatch, tmp_path):
    commands = []
    process = FakeProcess(
        [
            "extracting speech segments...\n",
            "computing alignments...\n",
            "...done\n",
        ]
    )

    monkeypatch.setattr(sync_subtitles.shutil, "which", lambda _name: "ffs")

    def fake_popen(command, **kwargs):
        commands.append((command, kwargs))
        return process

    monkeypatch.setattr(sync_subtitles.subprocess, "Popen", fake_popen)
    output = []

    result = sync_subtitles.sync_subs_audio(
        tmp_path / "Movie.mkv",
        tmp_path / "Movie.en.srt",
        on_output=output.append,
    )

    assert result is True
    assert output == [
        "extracting speech segments...",
        "computing alignments...",
        "...done",
    ]
    assert commands[0][1]["stderr"] is sync_subtitles.subprocess.STDOUT
    assert commands[0][1]["text"] is True


def test_sync_subs_audio_keeps_direct_terminal_output_without_callback(
    monkeypatch,
    tmp_path,
):
    calls = []
    monkeypatch.setattr(sync_subtitles.shutil, "which", lambda _name: "ffs")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(sync_subtitles.subprocess, "run", fake_run)

    sync_subtitles.sync_subs_audio(
        Path(tmp_path / "Movie.mkv"),
        Path(tmp_path / "Movie.en.srt"),
    )

    assert calls[0][1] == {"check": True}


def test_sync_subs_audio_finds_canonical_launcher_beside_python(
    monkeypatch,
    tmp_path,
):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / "python.exe"
    launcher = scripts / "ffsubsync.exe"
    python.touch()
    launcher.touch()
    calls = []

    monkeypatch.setattr(
        sync_subtitles,
        "sys",
        SimpleNamespace(executable=str(python)),
        raising=False,
    )
    monkeypatch.setattr(sync_subtitles.shutil, "which", lambda _name: None)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(sync_subtitles.subprocess, "run", fake_run)

    sync_subtitles.sync_subs_audio(
        tmp_path / "Movie.mkv",
        tmp_path / "Movie.en.srt",
    )

    assert calls[0][0][0] == str(launcher)


def test_iter_process_output_splits_carriage_return_progress():
    import io

    from library.sync_subtitles import _iter_process_output

    stream = io.StringIO(
        "line one\r 10%...\r 45%...\r\nfinal\rpartial-no-newline"
    )
    assert list(_iter_process_output(stream)) == [
        "line one",
        " 10%...",
        " 45%...",
        "final",
        "partial-no-newline",
    ]


def test_sync_cancel_event_terminates_the_subprocess(tmp_path, monkeypatch):
    import threading

    from library import sync_subtitles

    slow_stub = tmp_path / "slow_ffs.py"
    slow_stub.write_text(
        "import time\n"
        "print('progress 0%')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "ffs.cmd"
    launcher.write_text(
        f'@echo off\r\n"{__import__("sys").executable}" "{slow_stub}"\r\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_subtitles, "_find_ffsubsync", lambda: str(launcher))

    cancel = threading.Event()
    lines = []

    import threading as _t

    def cancel_soon():
        _t.Event().wait(0.5)
        cancel.set()

    _t.Thread(target=cancel_soon, daemon=True).start()

    import pytest as _pytest

    with _pytest.raises(Exception) as excinfo:
        sync_subtitles.sync_subs_audio(
            tmp_path / "movie.mkv",
            tmp_path / "movie.ar.srt",
            on_output=lines.append,
            cancel_event=cancel,
        )
    assert "cancelled" in str(excinfo.value).lower()
