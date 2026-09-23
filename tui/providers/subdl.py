"""SubDL adapter."""

from pathlib import Path

from tui.domain import Candidate, DownloadResult, Provider
from tui.providers.base import StandardProviderAdapter, redact_secrets


class SubDLAdapter(StandardProviderAdapter):
    provider = Provider.SUBDL

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
        try:
            if download_limits is None:
                path = self.client.download_single_subtitle(
                    candidate.download_ref,
                    media_path,
                    candidate.language,
                )
            else:
                path = self.client.download_single_subtitle(
                    candidate.download_ref,
                    media_path,
                    candidate.language,
                    download_limits=download_limits,
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
            subtitle_path=Path(path) if path else None,
            error=None if path else "Provider did not produce a subtitle file",
        )
