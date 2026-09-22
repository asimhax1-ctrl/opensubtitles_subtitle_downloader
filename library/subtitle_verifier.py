"""Post-download subtitle verification.

Checks that a staged subtitle is worth saving before it replaces an existing
file. Failures are reported back through DownloadResult so the caller can decide
whether to try another candidate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of verifying a staged subtitle."""

    passed: bool
    reason: str | None = None


class SubtitleVerifier:
    """Validate a subtitle file before it is committed beside the media."""

    MIN_COVERAGE_RATIO = 0.5
    MAX_OVERRUN_SECONDS = 300

    _ARABIC_RE = re.compile(
        r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
        r"\uFB50-\uFDFF\uFE70-\uFEFF]"
    )
    _LATIN_RE = re.compile(r"[A-Za-z]")

    def verify(
        self,
        subtitle_path: Path,
        media_path: Path,
        language: str | None,
    ) -> VerificationResult:
        """Return passed=True when the subtitle looks correct for the request."""
        try:
            text = self._read_text(subtitle_path)
        except OSError as exc:
            return VerificationResult(False, f"Cannot read subtitle: {exc}")

        stripped = text.strip()
        if not stripped:
            return VerificationResult(False, "Subtitle file is empty")

        fmt = self._detect_format(subtitle_path, text)
        if not self._format_valid(text, fmt):
            return VerificationResult(
                False,
                f"Subtitle format validation failed for {fmt or 'unknown'}",
            )

        if not self._language_valid(text, language):
            return VerificationResult(
                False,
                f"Subtitle language does not match requested language '{language}'",
            )

        coverage = self._coverage_valid(text, fmt, media_path)
        if not coverage.passed:
            return coverage

        return VerificationResult(True)

    @staticmethod
    def _read_text(path: Path) -> str:
        """Read a subtitle as text, tolerating a BOM."""
        data = path.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="replace")
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    @staticmethod
    def _detect_format(path: Path, text: str) -> str | None:
        """Guess subtitle format from extension or content."""
        ext = path.suffix.lower().lstrip(".")
        if ext in ("srt", "ass", "ssa", "vtt"):
            return ext
        if "[Script Info]" in text:
            return "ass"
        if text.lstrip().startswith("WEBVTT"):
            return "vtt"
        if "-->" in text:
            return "srt"
        return None

    @staticmethod
    def _format_valid(text: str, fmt: str | None) -> bool:
        """Sanity-check the file against the expected format."""
        if fmt == "srt":
            return "-->" in text
        if fmt in ("ass", "ssa"):
            return "[Script Info]" in text or "[V4" in text
        if fmt == "vtt":
            return text.lstrip().startswith("WEBVTT") or "-->" in text
        # Unknown format: cannot validate, assume valid.
        return True

    def _language_valid(self, text: str, language: str | None) -> bool:
        """Confirm the text contains characters of the requested language."""
        if language is None:
            return True
        code = language.lower().strip()
        if code == "ar":
            return bool(self._ARABIC_RE.search(text))
        if code == "en":
            return bool(self._LATIN_RE.search(text))
        # Other languages: no simple script check, skip.
        return True

    def _coverage_valid(
        self,
        text: str,
        fmt: str | None,
        media_path: Path,
    ) -> VerificationResult:
        """Check that the subtitle spans most of the video duration."""
        duration = self._video_duration(media_path)
        if duration is None or duration <= 0:
            return VerificationResult(True)

        last_cue = self._last_cue_seconds(text, fmt)
        if last_cue is None:
            return VerificationResult(True)

        if last_cue < self.MIN_COVERAGE_RATIO * duration:
            return VerificationResult(
                False,
                f"Subtitle duration {last_cue:.1f}s does not cover video duration "
                f"{duration:.1f}s",
            )
        if last_cue > duration + self.MAX_OVERRUN_SECONDS:
            return VerificationResult(
                False,
                f"Subtitle ends {last_cue - duration:.1f}s after video duration",
            )
        return VerificationResult(True)

    @staticmethod
    def _video_duration(media_path: Path) -> float | None:
        """Return video duration in seconds using ffprobe, or None if unavailable."""
        if not shutil.which("ffprobe"):
            return None
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                return None
            return float(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _last_cue_seconds(text: str, fmt: str | None) -> float | None:
        """Extract the last end timestamp from the subtitle, in seconds."""
        if fmt in ("srt", "vtt"):
            return SubtitleVerifier._last_srt_vtt_seconds(text)
        if fmt in ("ass", "ssa"):
            return SubtitleVerifier._last_ass_ssa_seconds(text)
        return None

    @staticmethod
    def _last_srt_vtt_seconds(text: str) -> float | None:
        last = None
        for line in text.splitlines():
            if "-->" not in line:
                continue
            parts = line.split("-->")
            if len(parts) < 2:
                continue
            end = parts[-1].strip().split()[0]
            seconds = SubtitleVerifier._time_to_seconds(end)
            if seconds is not None:
                last = max(last, seconds) if last is not None else seconds
        return last

    @staticmethod
    def _last_ass_ssa_seconds(text: str) -> float | None:
        last = None
        for line in text.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(",")
            if len(fields) < 3:
                continue
            seconds = SubtitleVerifier._time_to_seconds(fields[2].strip())
            if seconds is not None:
                last = max(last, seconds) if last is not None else seconds
        return last

    @staticmethod
    def _time_to_seconds(value: str) -> float | None:
        """Parse SRT/VTT/ASS time strings into seconds."""
        value = value.strip()
        if not value:
            return None
        # SRT/VTT: HH:MM:SS,mmm or HH:MM:SS.mmm
        # ASS/SSA: H:MM:SS.cc
        if "," in value:
            time_part, ms_part = value.rsplit(",", 1)
        elif "." in value:
            time_part, ms_part = value.rsplit(".", 1)
        else:
            time_part, ms_part = value, "0"
        try:
            parts = time_part.split(":")
            if len(parts) == 3:
                hours, minutes, secs = parts
                total = (
                    int(hours) * 3600
                    + int(minutes) * 60
                    + int(secs)
                    + int(ms_part) / 1000.0
                )
            elif len(parts) == 2:
                minutes, secs = parts
                total = int(minutes) * 60 + int(secs) + int(ms_part) / 1000.0
            else:
                return None
            return total
        except ValueError:
            return None
