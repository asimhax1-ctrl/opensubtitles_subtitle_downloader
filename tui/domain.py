"""Typed, provider-safe domain objects shared by the TUI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tui.compat import StrEnum


class Provider(StrEnum):
    OPENSUBTITLES = "opensubtitles"
    SUBDL = "subdl"
    SUBSOURCE = "subsource"

    @property
    def label(self) -> str:
        return {
            Provider.OPENSUBTITLES: "OpenSubtitles",
            Provider.SUBDL: "SubDL",
            Provider.SUBSOURCE: "SubSource",
        }[self]


class EngineMode(StrEnum):
    ASK = "ask"
    AUTO = "auto"
    ALL_PROVIDERS = "all-providers"
    OPENSUBTITLES = "opensubtitles"
    SUBDL = "subdl"
    SUBSOURCE = "subsource"

    @property
    def provider(self) -> Provider | None:
        try:
            return Provider(self.value)
        except ValueError:
            return None

    @property
    def label(self) -> str:
        if self is EngineMode.ALL_PROVIDERS:
            return "All providers"
        return self.provider.label if self.provider else self.value.title()


# Subtitle container formats this application recognizes end to end. Anything else
# is treated as unknown so callers fall back to content sniffing rather than guess.
SUBTITLE_FORMATS = ("srt", "ass", "ssa", "vtt", "sub")


def normalize_subtitle_format(value) -> str | None:
    """Return a known subtitle format from a format string, URL, or file name.

    Total: anything unrecognized returns ``None`` rather than a default, so a caller
    can distinguish "known to be SRT" from "unknown, sniff the file".
    """
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in SUBTITLE_FORMATS:
        return text
    leaf = text.rsplit("/", 1)[-1].rsplit("?", 1)[0].rsplit("#", 1)[0]
    if "." not in leaf:
        return None
    extension = leaf.rsplit(".", 1)[-1]
    return extension if extension in SUBTITLE_FORMATS else None


@dataclass(frozen=True)
class SearchRequest:
    media_path: Path | str
    query: str
    language: str
    hearing_impaired: str = "include"
    show_ai_translated: bool = True


@dataclass
class Candidate:
    provider: Provider
    provider_id: str
    release: str
    language: str
    download_ref: Any = field(default=None, repr=False)
    public_url: str | None = None
    download_count: int = 0
    hash_match: bool = False
    hearing_impaired: bool = False
    ai_translated: bool = False
    author: str = "Unknown"
    format: str | None = None
    match_reasons: tuple[str, ...] = ()
    score: float = 0.0
    # Verified compatibility with the media file, 0-100, and the evidence behind
    # it, computed by library.compatibility and rendered verbatim. It is what the
    # results are ordered by and what the panes explain, so the two cannot
    # disagree. ``score`` above is the older release-name score and no longer
    # ranks anything.
    compatibility: int = 0
    compatibility_badge: str = ""
    compatibility_evidence: tuple[str, ...] = ()
    compatibility_conflicts: tuple[str, ...] = ()
    raw_flags: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        return f"{self.provider.value}:{self.provider_id}"

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider.value,
            "provider_id": self.provider_id,
            "release": self.release,
            "language": self.language,
            "public_url": self.public_url,
            "download_count": self.download_count,
            "hash_match": self.hash_match,
            "hearing_impaired": self.hearing_impaired,
            "ai_translated": self.ai_translated,
            "author": self.author,
            "format": self.format,
            "match_reasons": list(self.match_reasons),
            "score": self.score,
            "compatibility": self.compatibility,
            "compatibility_badge": self.compatibility_badge,
            "compatibility_evidence": list(self.compatibility_evidence),
            "compatibility_conflicts": list(self.compatibility_conflicts),
        }


def compatibility_summary(candidate: Candidate) -> str:
    """The compatibility headline the panes show for one candidate.

    Both panes render this, so the two can never report a different verdict for
    the same candidate. A dash rather than "0% MISMATCH" when nothing was
    measured: not measuring a match is not the same as measuring no match.
    """
    if not candidate.compatibility_evidence:
        return "Compatibility: —"
    return (
        f"Compatibility: {candidate.compatibility}% {candidate.compatibility_badge}"
    )


@dataclass
class ProviderSearchResult:
    provider: Provider
    candidates: list[Candidate] = field(default_factory=list)
    error: str | None = None


@dataclass
class DownloadResult:
    provider: Provider
    media_path: Path
    subtitle_path: Path | None = None
    error: str | None = None
    conflict_path: Path | None = None
    # True when the failure is the verifier rejecting the downloaded subtitle
    # (bad coverage, wrong language, corrupt format): the candidate was bad, not
    # the request, so the caller may fall back to the next ranked candidate.
    verification_failed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.subtitle_path is not None and self.error is None


# Substrings marking a download failure as account/quota-wide rather than
# specific to the chosen candidate. Global failures must NOT trigger a walk
# down the ranked list: every candidate would fail the same way and burn API
# quota. Per-candidate failures (bad file_id, unreadable response, save error,
# verification rejection) may advance to the next untried candidate.
_GLOBAL_DOWNLOAD_FAILURE_MARKERS = (
    "authentication failed",
    "api key",
    "login failed",
    "not configured",
    "download limit reached",
    "quota",
    "429",
    "401",
    "403",
)


def is_global_download_failure(error: str | None) -> bool:
    """True when retrying another candidate cannot help (auth/quota/config)."""
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in _GLOBAL_DOWNLOAD_FAILURE_MARKERS)


def should_try_next_candidate(download: DownloadResult) -> bool:
    """True when the failure blames the candidate, not the request.

    Verification rejections always qualify. Other download errors qualify
    unless they are global (auth/quota/config): a bad file_id or an unreadable
    response for one row says nothing about the next row.
    """
    if download.succeeded or not download.error:
        return False
    if download.verification_failed:
        return True
    return not is_global_download_failure(download.error)


@dataclass
class PostProcessResult:
    utf8_normalized: bool = False
    cleaned: bool = False
    synced: bool = False
    utf8_error: str | None = None
    clean_error: str | None = None
    sync_error: str | None = None


@dataclass
class HealthResult:
    provider: Provider
    configured: bool
    reachable: bool
    authenticated: bool | None = None
    latency_ms: int | None = None
    reason: str | None = None


class QueueStatus(StrEnum):
    QUEUED = "queued"
    SEARCHING = "searching"
    AWAITING_PICK = "awaiting_pick"
    DOWNLOADING = "downloading"
    POST_PROCESSING = "post_processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class QueueItem:
    key: str
    path: Path
    language: str
    engine_mode: EngineMode
    status: QueueStatus = QueueStatus.QUEUED
    candidate_keys: list[str] = field(default_factory=list)
    selected_candidate_key: str | None = None
    error: str | None = None


@dataclass
class HistoryEntry:
    item_key: str
    media_path: Path
    candidate_key: str
    provider: Provider
    language: str
    subtitle_path: Path | None
    postprocess: PostProcessResult
    error: str | None = None
