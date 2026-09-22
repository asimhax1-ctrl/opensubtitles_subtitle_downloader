import pytest

from library.subtitle_verifier import SubtitleVerifier
from tui.domain import Candidate, DownloadResult, Provider
from tui.jobs import JobCoordinator, sniff_subtitle_format


def candidate_for(provider, provider_id="77"):
    return Candidate(
        provider=provider,
        provider_id=provider_id,
        release="Movie",
        language="en",
    )


def encoded_download(tmp_path, text, encoding):
    subtitle = tmp_path / "Movie.en.srt"
    subtitle.write_bytes(text.encode(encoding))
    return subtitle, DownloadResult(
        provider=Provider.SUBDL,
        media_path=tmp_path / "Movie.mkv",
        subtitle_path=subtitle,
    )


class RecordingAdapter:
    body = "new"
    extension = "srt"

    def __init__(self, provider):
        self.provider = provider
        self.downloads = []
        self.media_paths = []

    def download(self, candidate, media_path):
        self.downloads.append(candidate.key)
        self.media_paths.append(media_path)
        target = media_path.with_name(f"{media_path.stem}.en.{self.extension}")
        target.write_text(self.body, encoding="utf-8")
        return DownloadResult(
            provider=self.provider,
            media_path=media_path,
            subtitle_path=target,
        )


class FailingCleaner:
    def __init__(self, message):
        self.message = message

    def clean(self, subtitle_path, ads_path=None, ads_separator=","):
        raise RuntimeError(self.message)


class RecordingCleaner:
    def __init__(self):
        self.ads_paths = []
        self.separators = []

    def clean(self, subtitle_path, ads_path=None, ads_separator=","):
        self.ads_paths.append(ads_path)
        self.separators.append(ads_separator)
        return True


class StreamingSynchronizer:
    def sync(self, media_path, subtitle_path, on_output=None, **kwargs):
        on_output("extracting speech segments...")
        on_output("...done")
        return True


def test_download_dispatches_to_candidate_provider(tmp_path):
    adapters = {
        Provider.OPENSUBTITLES: RecordingAdapter(Provider.OPENSUBTITLES),
        Provider.SUBDL: RecordingAdapter(Provider.SUBDL),
    }
    candidate = candidate_for(Provider.SUBDL)

    result = JobCoordinator(adapters).download(candidate, tmp_path / "Movie.mkv")

    assert result.provider is Provider.SUBDL
    assert result.subtitle_path == tmp_path / "Movie.en.srt"
    assert adapters[Provider.SUBDL].downloads == [candidate.key]
    assert adapters[Provider.OPENSUBTITLES].downloads == []


def test_existing_target_requires_explicit_replace(tmp_path):
    adapters = {Provider.SUBDL: RecordingAdapter(Provider.SUBDL)}
    target = tmp_path / "Movie.en.srt"
    target.write_text("old", encoding="utf-8")

    result = JobCoordinator(adapters).download(
        candidate_for(Provider.SUBDL),
        tmp_path / "Movie.mkv",
    )

    assert result.conflict_path == target
    assert target.read_text(encoding="utf-8") == "old"
    assert adapters[Provider.SUBDL].downloads == []


def test_explicit_replace_is_atomic_from_staging(tmp_path):
    adapters = {Provider.SUBDL: RecordingAdapter(Provider.SUBDL)}
    target = tmp_path / "Movie.en.srt"
    target.write_text("old", encoding="utf-8")

    result = JobCoordinator(adapters).download(
        candidate_for(Provider.SUBDL),
        tmp_path / "Movie.mkv",
        overwrite=True,
    )

    assert result.succeeded
    assert target.read_text(encoding="utf-8") == "new"


def test_download_stages_and_saves_in_external_output_directory(tmp_path):
    adapter = RecordingAdapter(Provider.SUBDL)
    output_directory = tmp_path / "writable-subs"
    media = tmp_path / "read-only-library" / "Movie.mkv"

    result = JobCoordinator(
        {Provider.SUBDL: adapter},
        output_directory=output_directory,
    ).download(candidate_for(Provider.SUBDL), media)

    assert result.succeeded
    assert result.media_path == media
    assert result.subtitle_path == output_directory / "Movie.en.srt"
    assert adapter.media_paths[0].parent.parent == output_directory


def test_download_reports_unwritable_output_directory(tmp_path, monkeypatch):
    def deny_staging(*_args, **_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr("tui.jobs.TemporaryDirectory", deny_staging)
    result = JobCoordinator(
        {Provider.SUBDL: RecordingAdapter(Provider.SUBDL)},
        output_directory=tmp_path / "subtitles",
    ).download(
        candidate_for(Provider.SUBDL),
        tmp_path / "library" / "Movie.mkv",
    )


    assert result.succeeded is False
    assert "access denied" in result.error


def test_clean_failure_is_not_recorded_as_success(tmp_path):
    cleaner = FailingCleaner("bad ads pattern")
    download = DownloadResult(
        provider=Provider.SUBDL,
        media_path=tmp_path / "Movie.mkv",
        subtitle_path=tmp_path / "Movie.en.srt",
    )

    result = JobCoordinator({}, cleaner=cleaner).postprocess(
        download,
        clean=True,
        sync=False,
        ads_path=tmp_path / "ads.txt",
    )

    assert result.cleaned is False
    assert result.clean_error == "bad ads pattern"


def test_cleaner_receives_configured_ads_path(tmp_path):
    cleaner = RecordingCleaner()
    ads = tmp_path / "custom-ads.txt"
    download = DownloadResult(
        provider=Provider.SUBDL,
        media_path=tmp_path / "Movie.mkv",
        subtitle_path=tmp_path / "Movie.en.srt",
    )

    result = JobCoordinator({}, cleaner=cleaner).postprocess(
        download,
        clean=True,
        sync=False,
        ads_path=ads,
    )

    assert result.cleaned
    assert cleaner.ads_paths == [ads]


def test_force_utf8_normalizes_legacy_encoded_subtitle(tmp_path):
    subtitle = tmp_path / "Movie.en.srt"
    subtitle.write_bytes("café".encode("cp1252"))
    download = DownloadResult(
        provider=Provider.SUBDL,
        media_path=tmp_path / "Movie.mkv",
        subtitle_path=subtitle,
    )

    result = JobCoordinator({}).postprocess(
        download,
        force_utf8=True,
        clean=False,
        sync=False,
    )

    assert result.utf8_normalized
    assert subtitle.read_text(encoding="utf-8") == "café"


def test_force_utf8_normalizes_utf16_subtitle(tmp_path):
    subtitle, download = encoded_download(tmp_path, "hello", "utf-16")

    result = JobCoordinator({}).postprocess(
        download,
        force_utf8=True,
        clean=False,
        sync=False,
    )

    assert result.utf8_normalized
    assert subtitle.read_bytes() == b"hello"


def test_force_utf8_detects_windows_1256_subtitle(tmp_path):
    text = (
        "1\n00:00:01,000 --> 00:00:04,000\n"
        "مرحبا بكم في هذا الفيلم الرائع، نأمل أن تستمتعوا بالمشاهدة.\n\n"
        "2\n00:00:05,000 --> 00:00:09,000\n"
        "هذه جملة عربية طويلة لاختبار ترميز النصوص القديمة.\n"
    )
    subtitle, download = encoded_download(tmp_path, text, "windows-1256")

    result = JobCoordinator({}).postprocess(
        download,
        force_utf8=True,
        clean=False,
        sync=False,
    )

    assert result.utf8_normalized
    assert subtitle.read_text(encoding="utf-8") == text


def test_disabled_force_utf8_preserves_original_bytes(tmp_path):
    original = "café".encode("cp1252")
    subtitle, download = encoded_download(tmp_path, "café", "cp1252")

    result = JobCoordinator({}).postprocess(
        download,
        force_utf8=False,
        clean=False,
        sync=False,
    )

    assert result.utf8_normalized is False
    assert subtitle.read_bytes() == original


def test_postprocess_forwards_live_sync_output(tmp_path):
    subtitle = tmp_path / "Movie.en.srt"
    subtitle.write_text("subtitle", encoding="utf-8")
    download = DownloadResult(
        provider=Provider.SUBDL,
        media_path=tmp_path / "Movie.mkv",
        subtitle_path=subtitle,
    )
    output = []

    result = JobCoordinator(
        {},
        synchronizer=StreamingSynchronizer(),
    ).postprocess(
        download,
        clean=False,
        sync=True,
        sync_output=output.append,
    )

    assert result.synced is True
    assert output == ["extracting speech segments...", "...done"]


# --- Format sniffing ------------------------------------------------------

ASS_BODY = "[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\nFormat: Name\n"
SSA_BODY = "[Script Info]\nScriptType: v4.00\n[V4 Styles]\nFormat: Name\n"
SRT_BODY = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def format_candidate(subtitle_format=None, language="en"):
    return Candidate(
        provider=Provider.SUBDL,
        provider_id="77",
        release="Movie",
        language=language,
        format=subtitle_format,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [(ASS_BODY, "ass"), (SSA_BODY, "ssa"), (SRT_BODY, "srt"), ("", "srt")],
)
def test_sniff_subtitle_format_maps_content_to_extension(tmp_path, body, expected):
    path = tmp_path / "subtitle.bin"
    path.write_text(body, encoding="utf-8")

    assert sniff_subtitle_format(path) == expected


def test_an_ssa_body_wins_over_the_shared_script_info_marker(tmp_path):
    # SSA files also carry [Script Info]; the SSA-specific marker must be tested first.
    path = tmp_path / "subtitle.bin"
    path.write_text(SSA_BODY, encoding="utf-8")

    assert sniff_subtitle_format(path) == "ssa"


def test_known_candidate_format_is_honoured_without_sniffing(tmp_path):
    # The body is SRT, so a sniff would say "srt". Trusting the provider's "ass"
    # over the content is what proves no sniffing happened.
    adapter = RecordingAdapter(Provider.SUBDL)
    adapter.body = SRT_BODY
    jobs = JobCoordinator({Provider.SUBDL: adapter})

    result = jobs.download(format_candidate("ass"), tmp_path / "Movie.mkv")

    assert result.subtitle_path.suffix == ".ass"


def test_unknown_format_falls_back_to_content_sniffing(tmp_path):
    adapter = RecordingAdapter(Provider.SUBDL)
    adapter.body = SSA_BODY
    jobs = JobCoordinator({Provider.SUBDL: adapter})

    result = jobs.download(format_candidate(None), tmp_path / "Movie.mkv")

    assert result.subtitle_path.suffix == ".ssa"


def test_existing_ass_subtitle_is_detected_as_a_conflict(tmp_path):
    adapter = RecordingAdapter(Provider.SUBDL)
    jobs = JobCoordinator({Provider.SUBDL: adapter})
    media = tmp_path / "Movie.mkv"
    media.touch()
    existing = tmp_path / "Movie.ar.ass"
    existing.write_text(ASS_BODY, encoding="utf-8")

    result = jobs.download(format_candidate(None, language="ar"), media)

    assert result.conflict_path == existing
    assert existing.read_text(encoding="utf-8") == ASS_BODY
    assert adapter.downloads == []


def test_verification_failure_prevents_save(tmp_path):
    adapter = RecordingAdapter(Provider.SUBDL)
    adapter.body = "not a subtitle"
    jobs = JobCoordinator(
        {Provider.SUBDL: adapter},
        verifier=SubtitleVerifier(),
    )
    media = tmp_path / "Movie.mkv"
    media.touch()

    result = jobs.download(candidate_for(Provider.SUBDL), media)

    assert result.succeeded is False
    assert "format" in (result.error or "").lower()
    assert result.verification_failed is True
    assert not (tmp_path / "Movie.en.srt").exists()


def test_verified_subtitle_is_saved(tmp_path):
    adapter = RecordingAdapter(Provider.SUBDL)
    adapter.body = (
        "1\n00:00:01,000 --> 00:00:03,000\nhello world\n"
        "2\n00:09:55,000 --> 00:10:00,000\nbye\n"
    )
    jobs = JobCoordinator(
        {Provider.SUBDL: adapter},
        verifier=SubtitleVerifier(),
    )
    media = tmp_path / "Movie.mkv"
    media.touch()

    result = jobs.download(candidate_for(Provider.SUBDL), media)

    assert result.succeeded
    assert result.subtitle_path == tmp_path / "Movie.en.srt"
    assert "hello world" in result.subtitle_path.read_text(encoding="utf-8")
