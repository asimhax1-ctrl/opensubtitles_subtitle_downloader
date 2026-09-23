import os
import re
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory


def _find_ffsubsync() -> str:
    names = ("ffs", "ffsubsync")
    for name in names:
        if executable := shutil.which(name):
            return executable

    script_directories = [Path(sys.executable).parent]
    for scheme in (
        sysconfig.get_default_scheme(),
        sysconfig.get_preferred_scheme("user"),
    ):
        try:
            scripts = sysconfig.get_path("scripts", scheme=scheme)
        except (KeyError, TypeError):
            continue
        if scripts:
            script_directories.append(Path(scripts))

    suffixes = ("", ".exe")
    for directory in dict.fromkeys(script_directories):
        for name in names:
            for suffix in suffixes:
                candidate = directory / f"{name}{suffix}"
                if candidate.is_file():
                    return str(candidate)

    raise RuntimeError(
        "ffsubsync launcher was not found (checked 'ffs' and 'ffsubsync'). "
        f'Install it for this Python with: "{sys.executable}" '
        "-m pip install ffsubsync"
    )


def sync_subs_srt(_reference_srt, _unsync_srt, _output):
    _command = [
        _find_ffsubsync(),
        f"{_reference_srt}",
        "-i",
        f"{_unsync_srt}",
        "-o",
        f"{_output}",
    ]
    subprocess.call(_command)


def _iter_process_output(stream):
    """Yield process output split on both newlines and carriage returns.

    ffmpeg and tqdm report progress by rewriting a \\r-terminated line, so a
    plain ``for line in stream`` shows nothing for minutes on a large file.
    Splitting on \\r too surfaces that progress as it happens. Streams that do
    not support character reads (test fakes, pipes wrapped elsewhere) fall back
    to line iteration.
    """
    if not hasattr(stream, "read"):
        for line in stream:
            yield line.rstrip("\r\n")
        return
    buffer = ""
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in "\r\n":
            if buffer:
                yield buffer
                buffer = ""
        else:
            buffer += chunk
    if buffer:
        yield buffer


_CLOCK_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{2}):"
    r"(?P<seconds>\d{2})[,.](?P<fraction>\d{1,3})$"
)
_MICRODVD_CUE_RE = re.compile(r"^\{(\d+)\}\{(\d+)\}(.+)$")
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _clock_seconds(value: str) -> float | None:
    match = _CLOCK_RE.fullmatch(value.strip())
    if match is None:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    fraction = int(match.group("fraction").ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + fraction / 1000


def _visible_text(value: str) -> str:
    value = _ASS_OVERRIDE_RE.sub("", value)
    value = value.replace(r"\N", " ").replace(r"\n", " ")
    value = value.replace(r"\h", " ")
    return _HTML_TAG_RE.sub("", value).strip()


def _valid_timed_cue_count(text: str, subtitle_format: str) -> int:
    """Validate the sync output's actual format and return its valid cue count.

    This intentionally validates structure only. A valid cue timeline is not
    evidence that ffsubsync aligned it to speech correctly.
    """
    if subtitle_format in {"srt", "vtt"}:
        lines = text.splitlines()
        if subtitle_format == "vtt":
            if not lines or not lines[0].lstrip("\ufeff").startswith("WEBVTT"):
                raise RuntimeError("ffsubsync output is not valid VTT")
            lines = lines[1:]

        blocks = re.split(r"\r?\n\s*\r?\n", "\n".join(lines))
        cue_count = 0
        for block in blocks:
            rows = [row.strip() for row in block.splitlines() if row.strip()]
            if not rows:
                continue
            if subtitle_format == "vtt" and rows[0].startswith(
                ("NOTE", "STYLE", "REGION")
            ):
                continue
            timestamp_index = next(
                (index for index, row in enumerate(rows) if "-->" in row),
                None,
            )
            if timestamp_index is None:
                if subtitle_format == "vtt" and len(rows) == 1:
                    # VTT cue identifiers are separated from their timing line
                    # only when the following line exists; a lone identifier is
                    # an incomplete cue block.
                    raise RuntimeError("ffsubsync output contains a malformed VTT cue")
                raise RuntimeError(
                    "ffsubsync output contains malformed "
                    f"{subtitle_format.upper()} data"
                )
            if rows.count(rows[timestamp_index]) != 1:
                raise RuntimeError("ffsubsync output contains duplicate cue timestamps")
            timing = rows[timestamp_index].split("-->", 1)
            start = _clock_seconds(timing[0])
            end_token = timing[1].split(maxsplit=1)[0] if timing[1].split() else ""
            end = _clock_seconds(end_token)
            cue_text = " ".join(rows[timestamp_index + 1 :])
            if (
                start is None
                or end is None
                or end <= start
                or not _visible_text(cue_text)
            ):
                raise RuntimeError("ffsubsync output contains an invalid timed cue")
            cue_count += 1
        return cue_count

    if subtitle_format in {"ass", "ssa"}:
        lines = text.splitlines()
        has_script_info = any(line.strip().lower() == "[script info]" for line in lines)
        required_styles = "[v4+ styles]" if subtitle_format == "ass" else "[v4 styles]"
        has_styles = any(line.strip().lower() == required_styles for line in lines)
        if not has_script_info or not has_styles:
            raise RuntimeError(
                f"ffsubsync output is not valid {subtitle_format.upper()}"
            )

        section = ""
        event_fields: list[str] | None = None
        cue_count = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.lower()
                continue
            if section != "[events]":
                continue
            if stripped.lower().startswith("format:"):
                event_fields = [
                    field.strip().lower()
                    for field in stripped.split(":", 1)[1].split(",")
                ]
                continue
            if not stripped.lower().startswith("dialogue:"):
                continue
            if event_fields is None or not {"start", "end", "text"}.issubset(
                event_fields
            ):
                raise RuntimeError(
                    "ffsubsync output has invalid "
                    f"{subtitle_format.upper()} event fields"
                )
            values = (
                stripped.split(":", 1)[1]
                .lstrip()
                .split(",", len(event_fields) - 1)
            )
            if len(values) != len(event_fields):
                raise RuntimeError(
                    "ffsubsync output contains malformed "
                    f"{subtitle_format.upper()} dialogue"
                )
            start = _clock_seconds(values[event_fields.index("start")])
            end = _clock_seconds(values[event_fields.index("end")])
            if (
                start is None
                or end is None
                or end <= start
                or not _visible_text(values[event_fields.index("text")])
            ):
                raise RuntimeError("ffsubsync output contains an invalid timed cue")
            cue_count += 1
        return cue_count

    if subtitle_format == "sub":
        cue_count = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            match = _MICRODVD_CUE_RE.fullmatch(line.strip())
            if match is None or int(match.group(2)) <= int(match.group(1)):
                raise RuntimeError("ffsubsync output contains malformed MicroDVD cues")
            if not _visible_text(match.group(3)):
                raise RuntimeError("ffsubsync output contains an empty MicroDVD cue")
            cue_count += 1
        return cue_count

    raise RuntimeError(
        f"Cannot validate synchronized subtitle format: {subtitle_format}"
    )


def _validate_sync_output(output_path: Path, subtitle_format: str) -> None:
    if not output_path.is_file():
        raise RuntimeError("ffsubsync did not create an output file")
    if output_path.stat().st_size == 0:
        raise RuntimeError("ffsubsync produced an empty output file")
    try:
        text = output_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"ffsubsync output cannot be decoded: {exc}") from exc
    if _valid_timed_cue_count(text, subtitle_format) < 1:
        raise RuntimeError("ffsubsync output contains no valid timed cues")


def sync_subs_audio(
    media_path,
    subtitle_path,
    *,
    on_output: Callable[[str], None] | None = None,
    cancel_event=None,
):
    media_path = Path(media_path)
    subtitle_path = Path(subtitle_path)

    media_path = media_path.resolve()
    subtitle_path = subtitle_path.resolve()
    subtitle_format = subtitle_path.suffix.lower().lstrip(".")
    executable = _find_ffsubsync()
    with TemporaryDirectory(prefix=".sync-", dir=subtitle_path.parent) as temporary:
        output_path = Path(temporary) / f"synced{subtitle_path.suffix}"
        _command = [
            executable,
            str(media_path),
            "-i",
            str(subtitle_path),
            "-o",
            str(output_path),
            "--encoding",
            "utf-8",
        ]

        if on_output is None:
            subprocess.run(_command, check=True)
        else:
            process = subprocess.Popen(
                _command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            if cancel_event is not None:
                # A user-facing cancel must reach a subprocess that is mid-extraction:
                # terminate it the moment the event is set, then let the read loop
                # observe the closed pipe and report the cancellation.
                import threading

                def _watch_cancel():
                    cancel_event.wait()
                    if process.poll() is None:
                        process.terminate()

                threading.Thread(target=_watch_cancel, daemon=True).start()
            if process.stdout is not None:
                for line in _iter_process_output(process.stdout):
                    on_output(line)
            returncode = process.wait()
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Subtitle sync was cancelled")
            if returncode:
                raise subprocess.CalledProcessError(returncode, _command)

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Subtitle sync was cancelled")
        _validate_sync_output(output_path, subtitle_format)
        os.replace(output_path, subtitle_path)

    if on_output is None:
        print(f"{subtitle_path.absolute()} synced!")
    return True


if __name__ == "__main__":
    print("This is a Module")
