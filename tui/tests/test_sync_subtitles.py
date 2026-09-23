from pathlib import Path
from types import SimpleNamespace

import pytest

from library import sync_subtitles

ORIGINAL_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\nOriginal line\n"
    "\n2\n00:00:04,000 --> 00:00:05,000\nSecond line\n"
)
SYNCED_SRT = (
    "1\n00:00:01,500 --> 00:00:02,500\nSynced line\n"
    "\n2\n00:00:04,500 --> 00:00:05,500\nSecond line\n"
)
ASS_TEMPLATE = """[Script Info]
Title: Example
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, \
BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, \
MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,1,1,0,2,\
10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Example line
"""


def _make_srt(path, text=ORIGINAL_SRT):
    path.write_text(text, encoding="utf-8")
    return path


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_sync_subs_audio_streams_combined_ffsubsync_output(monkeypatch, tmp_path):
    commands = []
    subtitle = _make_srt(tmp_path / "Movie.en.srt")
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
        Path(command[command.index("-o") + 1]).write_text(
            SYNCED_SRT,
            encoding="utf-8",
        )
        return process

    monkeypatch.setattr(sync_subtitles.subprocess, "Popen", fake_popen)
    output = []

    result = sync_subtitles.sync_subs_audio(
        tmp_path / "Movie.mkv",
        subtitle,
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
    assert commands[0][0][commands[0][0].index("-i") + 1] == str(subtitle)
    output_path = Path(commands[0][0][commands[0][0].index("-o") + 1])
    assert output_path != subtitle
    assert output_path.suffix == subtitle.suffix
    assert subtitle.read_text(encoding="utf-8") == SYNCED_SRT
    assert not output_path.exists()


def test_sync_subs_audio_keeps_direct_terminal_output_without_callback(
    monkeypatch,
    tmp_path,
):
    calls = []
    subtitle = _make_srt(tmp_path / "Movie.en.srt")
    monkeypatch.setattr(sync_subtitles.shutil, "which", lambda _name: "ffs")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_text(
            SYNCED_SRT,
            encoding="utf-8",
        )

    monkeypatch.setattr(sync_subtitles.subprocess, "run", fake_run)

    sync_subtitles.sync_subs_audio(
        Path(tmp_path / "Movie.mkv"),
        subtitle,
    )

    assert calls[0][1] == {"check": True}
    assert subtitle.read_text(encoding="utf-8") == SYNCED_SRT
    output_path = Path(calls[0][0][calls[0][0].index("-o") + 1])
    assert output_path != subtitle
    assert output_path.suffix == subtitle.suffix


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
    subtitle = _make_srt(tmp_path / "Movie.en.srt")

    monkeypatch.setattr(
        sync_subtitles,
        "sys",
        SimpleNamespace(executable=str(python)),
        raising=False,
    )
    monkeypatch.setattr(sync_subtitles.shutil, "which", lambda _name: None)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_text(
            SYNCED_SRT,
            encoding="utf-8",
        )

    monkeypatch.setattr(sync_subtitles.subprocess, "run", fake_run)

    sync_subtitles.sync_subs_audio(
        tmp_path / "Movie.mkv",
        subtitle,
    )

    assert calls[0][0][0] == str(launcher)
    assert subtitle.read_text(encoding="utf-8") == SYNCED_SRT


def test_sync_subs_audio_preserves_original_when_engine_returns_empty_output(
    monkeypatch,
    tmp_path,
):
    media = tmp_path / "فيلم with spaces.wav"
    subtitle = tmp_path / "ترجمة with spaces.srt"
    original = ORIGINAL_SRT.encode("utf-8")
    media.write_bytes(b"controlled media fixture")
    subtitle.write_bytes(original)

    class EmptyOutputProcess(FakeProcess):
        def __init__(self, command):
            super().__init__([], returncode=0)
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_bytes(b"")

    monkeypatch.setattr(sync_subtitles, "_find_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(
        sync_subtitles.subprocess,
        "Popen",
        lambda command, **_kwargs: EmptyOutputProcess(command),
    )

    with pytest.raises(RuntimeError, match="empty|valid output"):
        sync_subtitles.sync_subs_audio(
            media,
            subtitle,
            on_output=lambda _line: None,
        )

    assert subtitle.read_bytes() == original


@pytest.mark.parametrize(
    "engine_output",
    [
        None,
        "",
        "not a subtitle file",
        "1\n00:00:00,000 --> 00:00:00,000\nZero duration\n",
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nWrong format\n",
    ],
    ids=("missing", "empty", "malformed", "no-valid-cue", "wrong-format"),
)
def test_sync_subs_audio_rejects_invalid_output_without_replacing_original(
    monkeypatch,
    tmp_path,
    engine_output,
):
    subtitle = _make_srt(tmp_path / "Movie.ar.srt")
    original = subtitle.read_bytes()

    def fake_popen(command, **_kwargs):
        if engine_output is not None:
            Path(command[command.index("-o") + 1]).write_text(
                engine_output,
                encoding="utf-8",
            )
        return FakeProcess([], returncode=0)

    monkeypatch.setattr(sync_subtitles, "_find_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(sync_subtitles.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError):
        sync_subtitles.sync_subs_audio(
            tmp_path / "Movie.mkv",
            subtitle,
            on_output=lambda _line: None,
        )

    assert subtitle.read_bytes() == original
    assert list(tmp_path.glob(".sync-*")) == []


def test_sync_subs_audio_nonzero_exit_preserves_original(monkeypatch, tmp_path):
    subtitle = _make_srt(tmp_path / "Movie.ar.srt")
    original = subtitle.read_bytes()

    def fake_popen(command, **_kwargs):
        Path(command[command.index("-o") + 1]).write_text(
            SYNCED_SRT,
            encoding="utf-8",
        )
        return FakeProcess([], returncode=17)

    monkeypatch.setattr(sync_subtitles, "_find_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(sync_subtitles.subprocess, "Popen", fake_popen)

    with pytest.raises(sync_subtitles.subprocess.CalledProcessError):
        sync_subtitles.sync_subs_audio(
            tmp_path / "Movie.mkv",
            subtitle,
            on_output=lambda _line: None,
        )

    assert subtitle.read_bytes() == original
    assert list(tmp_path.glob(".sync-*")) == []


@pytest.mark.parametrize(
    ("suffix", "original", "engine_output"),
    [
        (
            ".vtt",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nOriginal\n",
            "WEBVTT\n\n00:00:00.000 --> 00:00:00.000\nInvalid\n",
        ),
        (".sub", "{25}{50}Original line\n", None),
    ],
    ids=("invalid-vtt", "missing-microdvd-output"),
)
def test_sync_subs_audio_rejects_invalid_vtt_and_microdvd_output(
    monkeypatch,
    tmp_path,
    suffix,
    original,
    engine_output,
):
    subtitle = tmp_path / f"Movie.ar{suffix}"
    subtitle.write_text(original, encoding="utf-8")
    original_bytes = subtitle.read_bytes()

    def fake_popen(command, **_kwargs):
        if engine_output is not None:
            Path(command[command.index("-o") + 1]).write_text(
                engine_output,
                encoding="utf-8",
            )
        return FakeProcess([], returncode=0)

    monkeypatch.setattr(sync_subtitles, "_find_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(sync_subtitles.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError):
        sync_subtitles.sync_subs_audio(
            tmp_path / "Movie.mkv",
            subtitle,
            on_output=lambda _line: None,
        )

    assert subtitle.read_bytes() == original_bytes
    assert list(tmp_path.glob(".sync-*")) == []


@pytest.mark.parametrize(
    ("suffix", "contents"),
    [
        (".ass", ASS_TEMPLATE),
        (".ssa", ASS_TEMPLATE.replace("[V4+ Styles]", "[V4 Styles]")),
    ],
)
def test_sync_subs_audio_preserves_ass_and_ssa_format(
    monkeypatch,
    tmp_path,
    suffix,
    contents,
):
    subtitle = tmp_path / f"Movie.ar{suffix}"
    subtitle.write_text(contents, encoding="utf-8")
    output_content = contents.replace("Example line", "Synced line")

    def fake_run(command, **_kwargs):
        Path(command[command.index("-o") + 1]).write_text(
            output_content,
            encoding="utf-8",
        )

    monkeypatch.setattr(sync_subtitles, "_find_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(sync_subtitles.subprocess, "run", fake_run)

    assert sync_subtitles.sync_subs_audio(tmp_path / "Movie.mkv", subtitle)
    assert subtitle.read_text(encoding="utf-8") == output_content
    assert list(tmp_path.glob(".sync-*")) == []


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

    subtitle = _make_srt(tmp_path / "movie.ar.srt")
    original = subtitle.read_bytes()

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
            subtitle,
            on_output=lines.append,
            cancel_event=cancel,
        )
    assert "cancelled" in str(excinfo.value).lower()
    assert subtitle.read_bytes() == original
    assert list(tmp_path.glob(".sync-*")) == []
