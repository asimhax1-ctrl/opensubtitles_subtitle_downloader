from pathlib import Path

from tui.domain import (
    Candidate,
    DownloadResult,
    EngineMode,
    Provider,
    SearchRequest,
    compatibility_summary,
    is_global_download_failure,
    should_try_next_candidate,
)


def test_all_providers_is_a_backend_mode():
    assert EngineMode("all-providers") is EngineMode.ALL_PROVIDERS
    assert EngineMode.ALL_PROVIDERS.provider is None
    assert EngineMode.ALL_PROVIDERS.label == "All providers"


def test_candidate_key_is_provider_scoped():
    left = Candidate(
        provider=Provider.OPENSUBTITLES,
        provider_id="42",
        release="A",
        language="en",
    )
    right = Candidate(
        provider=Provider.SUBDL,
        provider_id="42",
        release="A",
        language="en",
    )

    assert left.key == "opensubtitles:42"
    assert right.key == "subdl:42"
    assert left.key != right.key


def test_candidate_public_mapping_excludes_private_download_reference():
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="7",
        release="Movie",
        language="en",
        download_ref={"url": "https://example.test/file.zip?api_key=secret"},
    )

    assert "download_ref" not in candidate.as_public_dict()
    assert "secret" not in repr(candidate)


def test_search_request_keeps_effective_query():
    request = SearchRequest(
        media_path=Path("Movie.mkv"),
        query="Director Cut",
        language="en",
    )

    assert request.query == "Director Cut"
    assert EngineMode.ASK.value == "ask"


def test_string_enums_serialize_as_their_values():
    assert str(Provider.SUBDL) == "subdl"
    assert str(EngineMode.AUTO) == "auto"


def test_candidate_public_dict_includes_format_and_match_reasons():
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
        format="ass",
        match_reasons=("exact hash match",),
    )

    public = candidate.as_public_dict()

    assert public["format"] == "ass"
    assert public["match_reasons"] == ["exact hash match"]


def test_candidate_public_dict_includes_compatibility_and_its_evidence():
    # The JSON view is read by the launcher's consumers, so the percentage that
    # orders the rows travels with the lines that justify it.
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
        compatibility=86,
        compatibility_badge="GREAT",
        compatibility_evidence=("Evidence:", "+ exact title"),
        compatibility_conflicts=(),
    )

    public = candidate.as_public_dict()

    assert public["compatibility"] == 86
    assert public["compatibility_badge"] == "GREAT"
    assert public["compatibility_evidence"] == ["Evidence:", "+ exact title"]
    assert public["compatibility_conflicts"] == []


def test_compatibility_summary_is_a_dash_when_nothing_was_measured():
    # A candidate the compatibility engine never saw must not be described as
    # "0% MISMATCH": not measuring a match is not the same as measuring no match.
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
    )

    assert compatibility_summary(candidate) == "Compatibility: —"


def test_compatibility_summary_names_the_badge_and_the_percentage():
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
        compatibility=42,
        compatibility_badge="MISMATCH",
        compatibility_evidence=("Conflict:", "media: Theatrical"),
    )

    assert compatibility_summary(candidate) == "Compatibility: 42% MISMATCH"


def test_candidate_defaults_are_format_none_and_no_reasons():
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
    )

    assert candidate.format is None
    assert candidate.match_reasons == ()


def test_quota_and_auth_errors_are_global_download_failures():
    assert is_global_download_failure(
        "RuntimeError: OpenSubtitles download limit reached (429)"
    )
    assert is_global_download_failure(
        "RuntimeError: OpenSubtitles authentication failed (401)"
    )
    assert is_global_download_failure(None) is False


def test_per_candidate_link_failure_is_not_global():
    assert (
        is_global_download_failure(
            "RuntimeError: Provider did not return a download link"
        )
        is False
    )


def _download(error, verification_failed=False):
    return DownloadResult(
        provider=Provider.OPENSUBTITLES,
        media_path=Path("Movie.mkv"),
        error=error,
        verification_failed=verification_failed,
    )


def test_should_try_next_candidate_for_per_candidate_failures():
    assert should_try_next_candidate(_download("bad file_id; try the next candidate"))
    assert should_try_next_candidate(_download("verification reason", True))


def test_should_not_try_next_candidate_for_global_failures():
    assert (
        should_try_next_candidate(_download("download limit reached (429)")) is False
    )
    assert (
        should_try_next_candidate(_download("authentication failed (401)")) is False
    )
