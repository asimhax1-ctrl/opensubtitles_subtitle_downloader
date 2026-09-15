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

    def download(self, candidate: Candidate, media_path: Path) -> DownloadResult:
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
                raise RuntimeError("Provider did not return a download link")
            if not self.client.save_subtitle(link, target):
                raise RuntimeError("Provider did not save the subtitle")
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
