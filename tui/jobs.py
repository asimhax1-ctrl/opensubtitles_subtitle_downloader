"""Source-aware download staging and truthful post-processing."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from charset_normalizer import from_bytes

from library.subtitle_verifier import SubtitleVerifier
from tui.domain import (
    SUBTITLE_FORMATS,
    Candidate,
    DownloadResult,
    PostProcessResult,
    Provider,
    normalize_subtitle_format,
)
from tui.providers.base import ProviderAdapter

# SSA-specific marker first: SSA files also carry "[Script Info]", so testing the
# ASS markers first would misclassify every SSA file as ASS.
SNIFF_SSA_MARKER = "[V4 Styles]"
SNIFF_ASS_MARKERS = ("[V4+ Styles]", "[Script Info]")
SNIFF_READ_BYTES = 4096
DEFAULT_SUBTITLE_FORMAT = "srt"


def sniff_subtitle_format(path: Path) -> str:
    """Infer a subtitle format from file content, defaulting to ``srt``.

    Only ever called inside the download staging directory, so an ambiguous result
    cannot leave a misnamed file beside the media.
    """
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            head = stream.read(SNIFF_READ_BYTES)
    except OSError:
        return DEFAULT_SUBTITLE_FORMAT
    if SNIFF_SSA_MARKER in head:
        return "ssa"
    if any(marker in head for marker in SNIFF_ASS_MARKERS):
        return "ass"
    return DEFAULT_SUBTITLE_FORMAT


class SubtitleCleaner:
    def clean(
        self,
        subtitle_path: Path,
        ads_path: Path | None = None,
        ads_separator: str = ",",
    ) -> bool:
        from library.subtitle_utils import SubtitleUtils

        return SubtitleUtils().clean_subtitles_strict(
            subtitle_path,
            ads_path=ads_path,
            ads_separator=ads_separator,
        )


class SubtitleSynchronizer:
    def sync(
        self,
        media_path: Path,
        subtitle_path: Path,
        on_output: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> bool:
        from library.subtitle_utils import SubtitleUtils

        return SubtitleUtils().sync_subtitles_strict(
            media_path,
            subtitle_path,
            on_output=on_output,
            cancel_event=cancel_event,
        )


class JobCoordinator:
    def __init__(
        self,
        adapters: dict[Provider, ProviderAdapter],
        *,
        cleaner: Any | None = None,
        synchronizer: Any | None = None,
        output_directory: str | Path | None = None,
        verifier: SubtitleVerifier | None = None,
    ) -> None:
        self.adapters = adapters
        self.cleaner = cleaner or SubtitleCleaner()
        self.synchronizer = synchronizer or SubtitleSynchronizer()
        self.output_directory = (
            Path(output_directory) if output_directory is not None else None
        )
        self.verifier = verifier

    def download(
        self,
        candidate: Candidate,
        media_path: str | Path,
        *,
        overwrite: bool = False,
        download_limits: Any | None = None,
    ) -> DownloadResult:
        media = Path(media_path)
        adapter = self.adapters.get(candidate.provider)
        if adapter is None:
            return DownloadResult(
                provider=candidate.provider,
                media_path=media,
                error=f"{candidate.provider.label} is not configured",
            )

        destination = self.output_directory or media.parent
        if self.output_directory is not None:
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    error=f"Could not create subtitle output directory: {exc}",
                )

        language_suffix = f".{candidate.language}" if candidate.language else ""
        if candidate.format:
            expected_names = [f"{media.stem}{language_suffix}.{candidate.format}"]
        else:
            expected_names = [
                f"{media.stem}{language_suffix}.{extension}"
                for extension in SUBTITLE_FORMATS
            ]
        for expected_name in expected_names:
            expected_target = destination / expected_name
            if expected_target.exists() and not overwrite:
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    conflict_path=expected_target,
                )

        try:
            return self._stage_download(
                adapter,
                candidate,
                media,
                destination,
                overwrite,
                self.verifier,
                download_limits,
            )
        except OSError as exc:
            return DownloadResult(
                provider=candidate.provider,
                media_path=media,
                error=f"Could not write subtitle output: {exc}",
            )

    @staticmethod
    def _stage_download(
        adapter: ProviderAdapter,
        candidate: Candidate,
        media: Path,
        destination: Path,
        overwrite: bool,
        verifier: SubtitleVerifier | None = None,
        download_limits: Any | None = None,
    ) -> DownloadResult:
        with TemporaryDirectory(
            prefix=".subtitle-download-",
            dir=destination,
        ) as temporary:
            staging_media = Path(temporary) / media.name
            if download_limits is None:
                staged = adapter.download(candidate, staging_media)
            else:
                staged = adapter.download(
                    candidate,
                    staging_media,
                    download_limits=download_limits,
                )
            if not staged.succeeded or staged.subtitle_path is None:
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    error=staged.error or "Subtitle download failed",
                )
            staged_path = staged.subtitle_path.resolve()
            staging_root = Path(temporary).resolve()
            if staged_path.parent != staging_root:
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    error="Provider wrote outside the download staging directory",
                )
            if (
                download_limits is not None
                and staged_path.stat().st_size > download_limits.max_payload_bytes
            ):
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    error="Subtitle payload exceeds the Auto download limit",
                )

            detected = normalize_subtitle_format(
                candidate.format
            ) or sniff_subtitle_format(staged_path)
            language_suffix = f".{candidate.language}" if candidate.language else ""
            desired_name = f"{media.stem}{language_suffix}.{detected}"
            if staged_path.name != desired_name:
                renamed = staged_path.with_name(desired_name)
                os.replace(staged_path, renamed)
                staged_path = renamed

            target = destination / staged_path.name
            if target.exists() and not overwrite:
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    conflict_path=target,
                )
            if verifier is not None:
                verification = verifier.verify(
                    staged_path, media, candidate.language
                )
                if not verification.passed:
                    return DownloadResult(
                        provider=candidate.provider,
                        media_path=media,
                        error=verification.reason or "Subtitle verification failed",
                        verification_failed=True,
                    )
            os.replace(staged_path, target)
            return DownloadResult(
                provider=candidate.provider,
                media_path=media,
                subtitle_path=target,
            )

    def postprocess(
        self,
        download: DownloadResult,
        *,
        force_utf8: bool = False,
        clean: bool,
        sync: bool,
        ads_path: Path | None = None,
        ads_separator: str = ",",
        sync_output: Callable[[str], None] | None = None,
        sync_cancel_event=None,
    ) -> PostProcessResult:
        result = PostProcessResult()
        if not download.succeeded or download.subtitle_path is None:
            return result
        if force_utf8:
            try:
                self._normalize_utf8(download.subtitle_path)
                result.utf8_normalized = True
            except Exception as exc:
                result.utf8_error = str(exc)
        if clean:
            try:
                cleaned = self.cleaner.clean(
                    download.subtitle_path,
                    ads_path=ads_path,
                    ads_separator=ads_separator,
                )
                if cleaned is not True:
                    raise RuntimeError("Cleaner did not report success")
                result.cleaned = True
            except Exception as exc:
                result.clean_error = str(exc)
        if sync:
            try:
                synced = self.synchronizer.sync(
                    download.media_path,
                    download.subtitle_path,
                    on_output=sync_output,
                    cancel_event=sync_cancel_event,
                )
                if synced is not True:
                    raise RuntimeError("Synchronizer did not report success")
                result.synced = True
            except Exception as exc:
                result.sync_error = str(exc)
        return result

    @staticmethod
    def _normalize_utf8(subtitle_path: Path) -> None:
        data = subtitle_path.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                match = from_bytes(data).best()
                encoding = match.encoding if match is not None else "cp1252"
                if encoding.lower().replace("-", "_").startswith(("utf_16", "utf_32")):
                    encoding = "cp1252"
                try:
                    text = data.decode(encoding)
                except (LookupError, UnicodeDecodeError):
                    text = data.decode("cp1252")
        temporary = subtitle_path.with_name(f".{subtitle_path.name}.utf8.tmp")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            os.replace(temporary, subtitle_path)
        finally:
            if temporary.exists():
                temporary.unlink()
