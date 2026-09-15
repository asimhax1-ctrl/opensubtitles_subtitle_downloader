"""Contract tests for the ARABIC_SUBS.bat Windows launcher.

The launcher is a drag-and-drop front end for the TUI: it must hand the dragged
path to ``download_subs.py`` untouched and always ask for Arabic across every
provider. Its two branches are tested differently -- the argument branch runs
the real batch file through ``cmd.exe`` against a stub app, and the picker
branch is only checked for structure, because a native file dialog cannot be
driven from a headless test run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "ARABIC_SUBS.bat"
PICKER_EXTENSIONS = ("mkv", "mp4", "avi", "mov", "m4v", "ts", "webm")

# Stands in for download_subs.py so the launcher can be observed without
# starting the TUI. It records the arguments it was handed, verbatim.
STUB_APP = """\
import json
import sys
from pathlib import Path

Path(__file__).with_name("argv.json").write_text(
    json.dumps(sys.argv[1:], ensure_ascii=False),
    encoding="utf-8",
)
"""


def _launcher_text() -> str:
    return LAUNCHER.read_text(encoding="ascii")


def _run_launcher(tmp_path: Path, argument: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the real launcher through cmd.exe against the stub app.

    The batch file locates the app relative to itself, so copying both into a
    temporary directory leaves the argument plumbing under test and nothing else.
    """
    (tmp_path / "ARABIC_SUBS.bat").write_bytes(LAUNCHER.read_bytes())
    (tmp_path / "download_subs.py").write_text(STUB_APP, encoding="utf-8")

    completed = subprocess.run(
        [os.environ.get("COMSPEC", "cmd"), "/c", str(tmp_path / "ARABIC_SUBS.bat"), argument],
        cwd=tmp_path,
        capture_output=True,
    )
    recorded = (tmp_path / "argv.json").read_text(encoding="utf-8")
    return completed, json.loads(recorded)


def test_launcher_exists_and_is_ascii_only():
    # cmd.exe reads a batch file's own bytes in the console code page, so a
    # non-ASCII literal anywhere in it -- a comment included -- would be decoded
    # as garbage and can break the surrounding parse.
    raw = LAUNCHER.read_bytes()

    assert raw.decode("ascii")


def test_launcher_asks_for_arabic_from_every_provider():
    text = _launcher_text()

    assert text.count("--lang") == text.count("--backend")
    assert "--lang ar" in text
    assert "--backend all-providers" in text


def test_launcher_quotes_the_dropped_path():
    assert '"%~1"' in _launcher_text()


def test_picker_offers_every_requested_video_extension():
    text = _launcher_text()

    for extension in PICKER_EXTENSIONS:
        assert f"*.{extension}" in text


def test_launcher_names_no_flag_that_would_download_without_asking():
    # The manual-selection requirement is satisfied by passing nothing beyond
    # --lang and --backend: auto_selection is a config value with no CLI flag.
    text = _launcher_text()

    for flag in ("--recursive", "--no-tui", "--output-dir", "--output-next-to-media"):
        assert flag not in text


def test_dropped_video_with_spaces_and_arabic_reaches_the_app_unchanged(tmp_path):
    media = tmp_path / "Amadeus (1984) عربي.mkv"
    media.touch()

    completed, argv = _run_launcher(tmp_path, str(media))

    assert argv == [str(media), "--lang", "ar", "--backend", "all-providers"]
    assert completed.returncode == 0


def test_dropped_folder_is_passed_through_unchanged(tmp_path):
    folder = tmp_path / "Movies عربي"
    folder.mkdir()

    completed, argv = _run_launcher(tmp_path, str(folder))

    assert argv == [str(folder), "--lang", "ar", "--backend", "all-providers"]
    assert completed.returncode == 0


@pytest.mark.parametrize("name", ["plain.mkv", "with space.mkv", "فيلم.mkv"])
def test_path_shapes_are_never_split_into_extra_arguments(tmp_path, name):
    media = tmp_path / name
    media.touch()

    _, argv = _run_launcher(tmp_path, str(media))

    # One path plus the four fixed flag tokens: a name that carries a space or
    # non-ASCII characters must not add arguments of its own.
    assert len(argv) == 5
    assert Path(argv[0]) == media
    assert argv[1:] == ["--lang", "ar", "--backend", "all-providers"]
