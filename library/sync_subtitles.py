import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from pathlib import Path


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
    # using subsync library to do the magic
    executable = _find_ffsubsync()
    _command = [
        executable,
        f"{media_path}",  # path to the video
        "-i",
        # the subtitle for input, using the same name as the film + .srt
        f"{subtitle_path}",
        "-o",
        f"{subtitle_path}",  # the output replaces the original subtitle
        "--encoding",
        "utf-8",
    ]  # encoding

    if on_output is None:
        subprocess.run(_command, check=True)
        print(f"{subtitle_path.absolute()} synced!")
        return True

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
        # terminate it the moment the event is set, then let the read loop see
        # the closed pipe and report the run as failed.
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
    return True


if __name__ == "__main__":
    print("This is a Module")
