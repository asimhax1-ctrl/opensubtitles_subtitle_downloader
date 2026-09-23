"""Bounded candidate staging and content-first Auto subtitle selection."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from library.subtitle_verifier import SubtitleVerifier
from tui.auto_quality import (
    QualityReport,
    SelectionDecision,
    choose_candidate,
    evaluate_subtitle,
)
from tui.domain import (
    Candidate,
    DownloadResult,
    PostProcessResult,
    Provider,
    is_global_download_failure,
)
from tui.jobs import JobCoordinator

SHORTLIST_LIMIT = 8
EVALUATION_LIMIT = 4
ATTEMPT_LIMIT = 8
AUTO_TEMP_PREFIX = ".auto-evaluation-"


@dataclass(frozen=True)
class DownloadLimits:
    max_payload_bytes: int = 64 * 1024 * 1024
    max_archive_members: int = 256
    max_member_bytes: int = 32 * 1024 * 1024


AUTO_DOWNLOAD_LIMITS = DownloadLimits()


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: Candidate
    quality: QualityReport
    staged_path: Path


@dataclass(frozen=True)
class AutoSelectionResult:
    decision: SelectionDecision
    evaluations: tuple[CandidateEvaluation, ...]
    attempts: int
    candidate: Candidate | None = None
    download: DownloadResult | None = None
    postprocess: PostProcessResult | None = None
    error: str | None = None
    provider_errors: tuple[str, ...] = ()


def shortlist_candidates(
    candidates: list[Candidate], requested_language: str
) -> list[Candidate]:
    """Return at most eight same-language candidates, with Fit last in ordering."""
    language = requested_language.strip().lower().replace("_", "-")
    eligible = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if candidate.language.strip().lower().replace("_", "-").split("-", 1)[0]
        == language.split("-", 1)[0]
    ]
    eligible.sort(
        key=lambda pair: (
            pair[1].hash_match,
            len(pair[1].match_reasons),
            bool(pair[1].format),
            pair[1].download_count,
            pair[1].compatibility if pair[1].compatibility_evidence else 0,
            -pair[0],
        ),
        reverse=True,
    )
    return [candidate for _, candidate in eligible[:SHORTLIST_LIMIT]]


class AutoSubtitleSelector:
    """Evaluate a bounded number of staged candidates and save only the winner."""

    def __init__(
        self,
        jobs: JobCoordinator,
        *,
        duration_provider: Callable[[Path], float | None] | None = None,
    ) -> None:
        self.jobs = jobs
        self.duration_provider = duration_provider or SubtitleVerifier._video_duration

    def run(
        self,
        candidates: list[Candidate],
        media_path: str | Path,
        *,
        requested_language: str,
        sync: bool,
        clean: bool,
        force_utf8: bool = False,
        ads_path: Path | None = None,
        ads_separator: str = ",",
        sync_output: Callable[[str], None] | None = None,
        sync_cancel_event: Any = None,
        sync_started: Callable[[Path], None] | None = None,
        cancel_event: Any = None,
    ) -> AutoSelectionResult:
        media = Path(media_path)
        try:
            duration = self.duration_provider(media)
        except Exception:
            duration = None
        staged: list[CandidateEvaluation] = []
        errors: list[str] = []
        attempts = 0
        blocked_providers: set[Provider] = set()
        shortlist = shortlist_candidates(candidates, requested_language)
        decision = SelectionDecision(
            None,
            True,
            "no same-language candidates are available",
        )

        try:
            with TemporaryDirectory(
                prefix=AUTO_TEMP_PREFIX,
                dir=media.parent,
            ) as temporary:
                root = Path(temporary)
                for candidate in shortlist:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if attempts >= ATTEMPT_LIMIT or len(staged) >= EVALUATION_LIMIT:
                        break
                    if candidate.provider in blocked_providers:
                        continue
                    attempt_dir = root / f"candidate-{attempts + 1}"
                    attempt_dir.mkdir()
                    attempt_jobs = JobCoordinator(
                        self.jobs.adapters,
                        output_directory=attempt_dir,
                        verifier=None,
                    )
                    attempts += 1
                    try:
                        downloaded = attempt_jobs.download(
                            candidate,
                            media,
                            download_limits=AUTO_DOWNLOAD_LIMITS,
                        )
                    except Exception as exc:
                        downloaded = DownloadResult(
                            candidate.provider,
                            media,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    if not downloaded.succeeded or downloaded.subtitle_path is None:
                        error = downloaded.error or "candidate download failed"
                        errors.append(f"{candidate.key}: {error}")
                        if is_global_download_failure(error):
                            blocked_providers.add(candidate.provider)
                        continue

                    try:
                        report = evaluate_subtitle(
                            downloaded.subtitle_path,
                            requested_language=requested_language,
                            media_duration=duration,
                            compatibility=candidate.compatibility,
                            compatibility_known=bool(candidate.compatibility_evidence),
                        )
                    except Exception as exc:
                        errors.append(
                            f"{candidate.key}: could not evaluate subtitle: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    staged.append(
                        CandidateEvaluation(
                            candidate,
                            report,
                            downloaded.subtitle_path,
                        )
                    )

                if cancel_event is not None and cancel_event.is_set():
                    decision = SelectionDecision(
                        None,
                        True,
                        "Auto selection was cancelled",
                    )
                else:
                    decision = choose_candidate([item.quality for item in staged])
                if decision.chosen_index is None:
                    return AutoSelectionResult(
                        decision,
                        tuple(staged),
                        attempts,
                        provider_errors=tuple(errors),
                    )

                winner = staged[decision.chosen_index]
                final_path = media.with_name(
                    f"{media.stem}.{requested_language}{winner.staged_path.suffix}"
                )
                if final_path.exists():
                    return AutoSelectionResult(
                        SelectionDecision(
                            None,
                            True,
                            f"subtitle already exists: {final_path}",
                            decision.rejected_indices,
                        ),
                        tuple(staged),
                        attempts,
                        candidate=winner.candidate,
                        error=f"Subtitle already exists: {final_path}",
                        provider_errors=tuple(errors),
                    )

                winner_download = DownloadResult(
                    provider=winner.candidate.provider,
                    media_path=media,
                    subtitle_path=winner.staged_path,
                )
                if sync and sync_started is not None:
                    sync_started(winner.staged_path)
                postprocess = self.jobs.postprocess(
                    winner_download,
                    force_utf8=force_utf8,
                    clean=clean,
                    sync=sync,
                    ads_path=ads_path,
                    ads_separator=ads_separator,
                    sync_output=sync_output,
                    sync_cancel_event=sync_cancel_event,
                )
                os.replace(winner.staged_path, final_path)
                final_download = DownloadResult(
                    provider=winner.candidate.provider,
                    media_path=media,
                    subtitle_path=final_path,
                )
                return AutoSelectionResult(
                    decision,
                    tuple(staged),
                    attempts,
                    candidate=winner.candidate,
                    download=final_download,
                    postprocess=postprocess,
                    provider_errors=tuple(errors),
                )
        except OSError as exc:
            return AutoSelectionResult(
                SelectionDecision(None, True, "Auto staging failed"),
                tuple(staged),
                attempts,
                error=f"Could not stage Auto candidates: {exc}",
                provider_errors=tuple(errors),
            )
