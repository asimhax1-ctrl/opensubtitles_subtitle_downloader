from unittest.mock import Mock

from library.SubSource import _SevenZipArchive
from tui.auto_selector import (
    AUTO_DOWNLOAD_LIMITS,
    AutoSubtitleSelector,
    shortlist_candidates,
)
from tui.domain import Candidate, DownloadResult, Provider
from tui.jobs import JobCoordinator


def _candidate(
    provider: Provider,
    provider_id: str,
    *,
    content: str | None = None,
    error: str | None = None,
    fit: int = 0,
    subtitle_format: str = "srt",
    hash_match: bool = False,
    download_count: int = 0,
):
    return Candidate(
        provider=provider,
        provider_id=provider_id,
        release=f"Release {provider_id}",
        language="ar",
        download_ref={"content": content, "error": error},
        format=subtitle_format,
        compatibility=fit,
        compatibility_evidence=("measured fit",) if fit else (),
        hash_match=hash_match,
        download_count=download_count,
        match_reasons=("title",) if hash_match else (),
    )


class _Adapter:
    def __init__(self, provider, attempts):
        self.provider = provider
        self.attempts = attempts

    def download(self, candidate, media_path, *, download_limits=None):
        self.attempts.append((candidate.key, download_limits))
        error = candidate.download_ref["error"]
        if error:
            return DownloadResult(
                provider=self.provider,
                media_path=media_path,
                error=error,
            )
        subtitle = media_path.with_name(
            f"{media_path.stem}.{candidate.language}.{candidate.format}"
        )
        subtitle.write_text(candidate.download_ref["content"], encoding="utf-8")
        return DownloadResult(
            provider=self.provider,
            media_path=media_path,
            subtitle_path=subtitle,
        )


def _arabic_srt(prefix: str, cues: int = 8) -> str:
    blocks = []
    for index in range(cues):
        start = index * 60 + 1
        end = start + 3
        blocks.append(
            f"{index + 1}\n00:{start // 60:02}:{start % 60:02},000 --> "
            f"00:{end // 60:02}:{end % 60:02},000\n"
            f"{prefix} جملة عربية للاختبار رقم {index}"
        )
    return "\n\n".join(blocks) + "\n"


def test_auto_download_limits_are_bounded():
    assert AUTO_DOWNLOAD_LIMITS.max_payload_bytes == 64 * 1024 * 1024
    assert AUTO_DOWNLOAD_LIMITS.max_archive_members == 256
    assert AUTO_DOWNLOAD_LIMITS.max_member_bytes == 32 * 1024 * 1024


def test_seven_zip_member_reader_never_reads_unbounded_remainder(monkeypatch):
    stdout = Mock()
    stdout.read.side_effect = [b"x" * 5, b"x" * 5, b""]
    process = Mock(stdout=stdout, returncode=0)
    monkeypatch.setattr(
        "library.SubSource.subprocess.Popen",
        Mock(return_value=process),
    )

    archive = object.__new__(_SevenZipArchive)
    archive._path = "archive.rar"
    archive._exe = "7z"
    data = archive.read_limited("sub.srt", 10)

    assert len(data) == 10
    assert all(call.args and call.args[0] >= 0 for call in stdout.read.call_args_list)


def test_cancelled_auto_selection_does_not_attempt_downloads_or_leak_temp_files(
    tmp_path,
):
    attempts = []
    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    candidate = _candidate(
        Provider.OPENSUBTITLES,
        "cancelled",
        content=_arabic_srt("نص"),
    )
    cancel = __import__("threading").Event()
    cancel.set()

    result = AutoSubtitleSelector(
        JobCoordinator(adapters),
        duration_provider=lambda _path: 480,
    ).run(
        [candidate],
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=False,
        clean=False,
        cancel_event=cancel,
    )

    assert result.candidate is None
    assert result.attempts == 0
    assert attempts == []
    assert not list(tmp_path.glob(".auto-evaluation-*"))


def test_shortlist_caps_eight_and_uses_fit_only_after_other_evidence():
    candidates = [
        _candidate(
            Provider.OPENSUBTITLES,
            str(index),
            fit=100 - index,
            hash_match=index == 8,
            download_count=100 - index,
        )
        for index in range(12)
    ]

    shortlist = shortlist_candidates(candidates, "ar")

    assert len(shortlist) == 8
    assert shortlist[0].provider_id == "8"


def test_one_download_failure_falls_through_and_evaluates_four_successes(tmp_path):
    attempts = []
    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    jobs = JobCoordinator(adapters)
    candidates = [
        _candidate(Provider.OPENSUBTITLES, "fail", error="temporary network failure"),
        *[
            _candidate(
                Provider.SUBDL,
                str(index),
                content=_arabic_srt(f"نسخة {index}"),
                fit=(100 if index == 1 else 0),
            )
            for index in range(1, 7)
        ],
    ]
    selector = AutoSubtitleSelector(jobs, duration_provider=lambda _path: 480)

    result = selector.run(
        candidates,
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=False,
        clean=False,
    )

    assert result.attempts == 5
    assert len(result.evaluations) == 4
    assert len(attempts) == 5
    assert all(limits == AUTO_DOWNLOAD_LIMITS for _, limits in attempts)
    assert result.candidate.provider_id == "1"
    assert result.download.subtitle_path == tmp_path / "Movie.ar.srt"
    assert result.download.subtitle_path.exists()
    assert not list(tmp_path.glob(".auto-evaluation-*"))


def test_provider_wide_failure_skips_that_provider_but_tries_another(tmp_path):
    attempts = []
    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    candidates = [
        _candidate(Provider.OPENSUBTITLES, "auth", error="authentication failed"),
        _candidate(Provider.OPENSUBTITLES, "same-provider", content=_arabic_srt("نص")),
        _candidate(Provider.SUBDL, "other", content=_arabic_srt("بديل")),
    ]
    selector = AutoSubtitleSelector(
        JobCoordinator(adapters),
        duration_provider=lambda _path: 480,
    )

    result = selector.run(
        candidates,
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=False,
        clean=False,
    )

    assert result.candidate.provider is Provider.SUBDL
    assert len(attempts) == 2
    assert not list(tmp_path.glob(".auto-evaluation-*"))


def test_ambiguous_results_leave_all_candidate_files_temporary(tmp_path):
    attempts = []
    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    long_line_blocks = _arabic_srt("نسخة", 8).strip().split("\n\n")
    long_line_blocks = [
        block.rsplit("\n", 1)[0] + "\n" + ("كلمة " * 24) for block in long_line_blocks
    ]
    malformed_half = _arabic_srt("ترجمة مختلفة", 8).strip().split("\n\n")
    malformed_half = [
        block if index % 2 == 0 else "malformed cue block"
        for index, block in enumerate(malformed_half)
    ]
    candidates = [
        _candidate(
            Provider.OPENSUBTITLES,
            "one",
            content="\n\n".join(long_line_blocks),
        ),
        _candidate(
            Provider.SUBDL,
            "two",
            content="\n\n".join(malformed_half),
        ),
    ]
    selector = AutoSubtitleSelector(
        JobCoordinator(adapters),
        duration_provider=lambda _path: 480,
    )

    result = selector.run(
        candidates,
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=False,
        clean=False,
    )

    assert result.decision.manual_required
    assert result.download is None
    assert not (tmp_path / "Movie.ar.srt").exists()
    assert not list(tmp_path.glob(".auto-evaluation-*"))


def test_sync_failure_preserves_and_saves_the_selected_original(tmp_path):
    attempts = []

    class BrokenSynchronizer:
        def sync(self, *_args, **_kwargs):
            raise RuntimeError("simulated ffsubsync failure")

    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    original = _arabic_srt("الفائز")
    candidate = _candidate(
        Provider.OPENSUBTITLES,
        "winner",
        content=original,
    )
    jobs = JobCoordinator(adapters, synchronizer=BrokenSynchronizer())

    result = AutoSubtitleSelector(
        jobs,
        duration_provider=lambda _path: 480,
    ).run(
        [candidate],
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=True,
        clean=False,
    )

    assert result.download.subtitle_path.suffix == ".srt"
    assert result.download.subtitle_path.read_text(encoding="utf-8") == original
    assert "simulated ffsubsync failure" in result.postprocess.sync_error
    assert not list(tmp_path.glob(".auto-evaluation-*"))


def test_auto_without_sync_never_invokes_the_synchronizer(tmp_path):
    attempts = []

    class MustNotSync:
        def sync(self, *_args, **_kwargs):
            raise AssertionError("sync was called while Auto Sync was disabled")

    candidate = _candidate(
        Provider.OPENSUBTITLES,
        "no-sync",
        content=_arabic_srt("النص المختار"),
    )
    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    result = AutoSubtitleSelector(
        JobCoordinator(adapters, synchronizer=MustNotSync()),
        duration_provider=lambda _path: 480,
    ).run(
        [candidate],
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=False,
        clean=False,
    )

    assert result.download is not None
    assert result.postprocess.synced is False
    assert result.download.subtitle_path.suffix == ".srt"


def test_selected_ass_is_saved_without_format_conversion(tmp_path):
    attempts = []
    adapters = {provider: _Adapter(provider, attempts) for provider in Provider}
    original = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,1,1,0,2,10,10,10,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,{\\b1}مرحبا بالعالم\n"
    )
    candidate = _candidate(
        Provider.OPENSUBTITLES,
        "ass",
        content=original,
        subtitle_format="ass",
    )

    result = AutoSubtitleSelector(
        JobCoordinator(adapters),
        duration_provider=lambda _path: 4,
    ).run(
        [candidate],
        tmp_path / "Movie.mkv",
        requested_language="ar",
        sync=False,
        clean=False,
    )

    assert result.download.subtitle_path.suffix == ".ass"
    assert result.download.subtitle_path.read_text(encoding="utf-8") == original
