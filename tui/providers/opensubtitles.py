"""OpenSubtitles adapter."""

from pathlib import Path

from tui.domain import (
    Candidate,
    DownloadResult,
    Provider,
    normalize_subtitle_format,
)
from tui.providers.base import StandardProviderAdapter, redact_secrets


class OpenSubtitlesAdapter(StandardProviderAdapter):
    provider = Provider.OPENSUBTITLES

    def download(
        self,
        candidate: Candidate,
        media_path: Path,
        *,
        download_limits=None,
    ) -> DownloadResult:
        invalid = self._invalid_candidate(candidate, media_path)
        if invalid:
            return invalid
        extension = normalize_subtitle_format(candidate.format) or "srt"
        target = media_path.with_name(
            f"{media_path.stem}.{candidate.language}.{extension}"
        )
        try:
            link = self.client.get_download_link(candidate.download_ref)
            if not link:
                raise RuntimeError(
                    "Provider did not return a download link. Try the next "
                    "candidate or All providers (m); if every OpenSubtitles "
                    "row fails, the API quota/login is the likely cause "
                    "(press r to probe)"
                )
            if download_limits is None:
                saved = self.client.save_subtitle(link, target)
            else:
                saved = self.client.save_subtitle(
                    link,
                    target,
                    max_bytes=download_limits.max_payload_bytes,
                )
            if not saved:
                raise RuntimeError(
                    "Provider returned a link but the subtitle could not be "
                    "saved. Try the next candidate"
                )
        except Exception as exc:
            return DownloadResult(
                provider=self.provider,
                media_path=media_path,
                error=redact_secrets(f"{type(exc).__name__}: {exc}"),
            )
        return DownloadResult(
            provider=self.provider,
            media_path=media_path,
            subtitle_path=target,
        )
