import threading

import pytest

from tui.domain import (
    Candidate,
    HealthResult,
    Provider,
    ProviderSearchResult,
    SearchRequest,
)
from tui.search import (
    AUTO_PRIORITY,
    MAX_MATCH_REASONS,
    PROVIDER_RELIABILITY,
    SearchCoordinator,
)

REQUEST = SearchRequest(
    media_path="Movie.2026.mkv",
    query="Movie 2026",
    language="en",
)


def candidate(provider, provider_id):
    return Candidate(
        provider=provider,
        provider_id=provider_id,
        release=f"Movie {provider_id}",
        language="en",
    )


class FakeAdapter:
    def __init__(self, provider, candidates=(), error=None):
        self.provider = provider
        self.candidates = list(candidates)
        self.error = error
        self.calls = 0

    def search(self, request):
        self.calls += 1
        return ProviderSearchResult(
            provider=self.provider,
            candidates=list(self.candidates),
            error=self.error,
        )


class BarrierAdapter(FakeAdapter):
    def __init__(self, provider, barrier):
        super().__init__(provider, [candidate(provider, "42")])
        self.barrier = barrier

    def search(self, request):
        self.barrier.wait(timeout=2)
        return super().search(request)


def test_all_providers_retains_same_raw_id_from_every_provider():
    adapters = {
        provider: FakeAdapter(provider, [candidate(provider, "42")])
        for provider in Provider
    }

    result = SearchCoordinator(adapters).all_providers(REQUEST)

    assert {item.key for item in result.candidates} == {
        "opensubtitles:42",
        "subdl:42",
        "subsource:42",
    }


def test_all_providers_retains_successes_and_reports_partial_failure():
    adapters = {
        Provider.OPENSUBTITLES: FakeAdapter(
            Provider.OPENSUBTITLES, error="network down"
        ),
        Provider.SUBDL: FakeAdapter(Provider.SUBDL, [candidate(Provider.SUBDL, "1")]),
    }

    result = SearchCoordinator(adapters).all_providers(REQUEST)

    assert [item.key for item in result.candidates] == ["subdl:1"]
    assert result.errors[Provider.OPENSUBTITLES] == "network down"


def test_all_providers_runs_provider_calls_concurrently():
    gate = threading.Barrier(3)
    adapters = {provider: BarrierAdapter(provider, gate) for provider in Provider}

    result = SearchCoordinator(adapters).all_providers(REQUEST)

    assert len(result.candidates) == 3


def test_auto_falls_back_after_error_and_empty_success():
    adapters = {
        Provider.SUBSOURCE: FakeAdapter(Provider.SUBSOURCE, error="down"),
        Provider.OPENSUBTITLES: FakeAdapter(Provider.OPENSUBTITLES),
        Provider.SUBDL: FakeAdapter(Provider.SUBDL, [candidate(Provider.SUBDL, "8")]),
    }

    result = SearchCoordinator(adapters).auto(REQUEST)

    assert result.selected_provider is Provider.SUBDL
    assert result.attempted == list(AUTO_PRIORITY)


def test_health_does_not_exclude_a_working_provider():
    adapters = {
        Provider.OPENSUBTITLES: FakeAdapter(
            Provider.OPENSUBTITLES,
            [candidate(Provider.OPENSUBTITLES, "1")],
        )
    }

    result = SearchCoordinator(adapters).all_providers(
        REQUEST,
        health={
            Provider.OPENSUBTITLES: HealthResult(
                provider=Provider.OPENSUBTITLES,
                configured=True,
                reachable=False,
                reason="probe failed",
            )
        },
    )

    assert result.candidates[0].provider is Provider.OPENSUBTITLES


def test_shared_filters_apply_to_all_providers():
    ai = candidate(Provider.SUBDL, "ai")
    ai.ai_translated = True
    hi = candidate(Provider.SUBSOURCE, "hi")
    hi.hearing_impaired = True
    normal = candidate(Provider.OPENSUBTITLES, "normal")
    adapters = {
        Provider.SUBDL: FakeAdapter(Provider.SUBDL, [ai]),
        Provider.SUBSOURCE: FakeAdapter(Provider.SUBSOURCE, [hi]),
        Provider.OPENSUBTITLES: FakeAdapter(Provider.OPENSUBTITLES, [normal]),
    }
    request = SearchRequest(
        media_path=REQUEST.media_path,
        query=REQUEST.query,
        language="en",
        hearing_impaired="exclude",
        show_ai_translated=False,
    )

    result = SearchCoordinator(adapters).all_providers(request)

    assert [item.key for item in result.candidates] == ["opensubtitles:normal"]


# --- Ranking and match reasons --------------------------------------------


class FixedScorer:
    """Duck-typed scorer: scores by release name, emits explainable reasons."""

    def __init__(self, scores=None, reasons=("movie/episode match",)):
        self.scores = scores or {}
        self.reasons = reasons

    def score_subtitle(self, release, target, hash_match=False):
        return float(self.scores.get(release, 50.0))

    def explain_subtitle_match(self, release, target, hash_match=False):
        from library.subtitle_utils import MatchExplanation

        score = 100.0 if hash_match else float(self.scores.get(release, 50.0))
        return MatchExplanation(score=score, reasons=tuple(self.reasons))


class LegacyScorer:
    """A scorer with no explain_subtitle_match, as existing tests inject."""

    def score_subtitle(self, release, target, hash_match=False):
        return 42.0


def _request(language="ar"):
    return SearchRequest(
        media_path="Movie.2026.mkv",
        query="Movie 2026",
        language=language,
    )


def _candidate(provider, provider_id, language, release=None):
    return Candidate(
        provider=provider,
        provider_id=provider_id,
        release=release or f"Movie {provider_id}",
        language=language,
    )


def test_target_language_outranks_a_higher_scoring_other_language():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "ar", "ar")],
        ),
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [_candidate(Provider.SUBSOURCE, "en", "en")],
        ),
    }
    scorer = FixedScorer(scores={"Movie en": 95.0, "Movie ar": 40.0})

    result = SearchCoordinator(adapters, scorer=scorer).all_providers(_request("ar"))

    assert result.candidates[0].language == "ar"


def test_provider_reliability_orders_equal_score_candidates():
    adapters = {
        provider: FakeAdapter(
            provider,
            [_candidate(provider, provider.value, "ar")],
        )
        for provider in Provider
    }

    result = SearchCoordinator(adapters, scorer=FixedScorer()).all_providers(
        _request("ar")
    )

    assert [item.provider for item in result.candidates] == [
        Provider.SUBSOURCE,
        Provider.OPENSUBTITLES,
        Provider.SUBDL,
    ]


def test_provider_reliability_values_are_derived_from_auto_priority():
    assert PROVIDER_RELIABILITY == {
        Provider.SUBSOURCE: 3,
        Provider.OPENSUBTITLES: 2,
        Provider.SUBDL: 1,
    }


def _media_request(media_name, language="ar"):
    return SearchRequest(
        media_path=media_name,
        query=media_name,
        language=language,
    )


def _release_candidate(provider, provider_id, release, language="ar", **overrides):
    return Candidate(
        provider=provider,
        provider_id=provider_id,
        release=release,
        language=language,
        **overrides,
    )


def _compatibilities(result):
    return [item.compatibility for item in result.candidates]


def test_results_are_ordered_by_the_compatibility_the_user_is_shown():
    # The number in the Fit column and the order of the rows come from the same
    # field, so the table cannot show an order its own percentages contradict.
    media = "Amadeus (1984) 2160p BluRay x264-GROUP"
    releases = [
        "Amadeus.1985.2160p.BluRay.x264-GROUP",  # wrong year
        "Amadeus.1984.2160p.BluRay.x264-GROUP",  # exact
        "Amadeus.1984.480p.DVDRip.xvid-GROUP",  # exact title, wrong source
        "Unrelated.1984.2160p.BluRay.x264-GROUP",  # wrong title
    ]
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [
                _release_candidate(Provider.SUBDL, str(index), release)
                for index, release in enumerate(releases)
            ],
        )
    }

    result = SearchCoordinator(adapters).all_providers(_media_request(media))

    shown = _compatibilities(result)
    assert shown == sorted(shown, reverse=True)
    assert result.candidates[0].release == releases[1]
    # A wrong title must not outrank a wrong year: identity dominates, so the
    # unrelated release sorts last rather than somewhere in the middle.
    assert result.candidates[-1].release == releases[3]


def test_an_exact_hash_outranks_every_filename_only_match():
    media = "Amadeus (1984) 2160p BluRay x264-GROUP"
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [
                _release_candidate(
                    Provider.SUBDL,
                    "by-name",
                    "Amadeus.1984.2160p.BluRay.x264-GROUP",
                ),
                # The weaker provider on purpose: the hash must win on evidence
                # alone, not on the reliability tiebreak behind it.
                _release_candidate(
                    Provider.SUBDL,
                    "by-hash",
                    "Amadeus.1984.2160p.BluRay.x264-GROUP",
                    hash_match=True,
                ),
            ],
        )
    }

    result = SearchCoordinator(adapters).all_providers(_media_request(media))

    assert result.candidates[0].provider_id == "by-hash"
    assert result.candidates[0].compatibility == 100
    assert result.candidates[0].compatibility > result.candidates[1].compatibility


def test_download_count_does_not_change_compatibility():
    # Popularity is not evidence about the file. Two things must hold: a heavily
    # downloaded release with weaker evidence may not overtake a stronger one, and
    # the percentage a release earns may not move with its download count.
    media = "Amadeus (1984) 2160p BluRay x264-GROUP"
    strong = "Amadeus.1984.2160p.BluRay.x264-GROUP"
    weak = "Amadeus.1985.480p.DVDRip.xvid-OTHER"
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [
                _release_candidate(
                    Provider.SUBDL, "popular-but-weak", weak, download_count=900_000
                ),
                _release_candidate(
                    Provider.SUBDL, "quiet-but-strong", strong, download_count=1
                ),
            ],
        )
    }

    result = SearchCoordinator(adapters).all_providers(_media_request(media))

    assert result.candidates[0].provider_id == "quiet-but-strong"
    assert result.candidates[0].compatibility > result.candidates[1].compatibility

    adapters[Provider.SUBDL] = FakeAdapter(
        Provider.SUBDL,
        [
            _release_candidate(
                Provider.SUBDL, "same-evidence", strong, download_count=900_000
            )
        ],
    )
    popular = SearchCoordinator(adapters).all_providers(_media_request(media))

    assert (
        popular.candidates[0].compatibility
        == result.candidates[0].compatibility
    )


def test_provider_reliability_only_separates_otherwise_equal_candidates():
    media = "Amadeus (1984) 2160p BluRay x264-GROUP"
    adapters = {
        # SubSource is the most reliable provider, SubDL the least, so a result
        # that wins here won on compatibility rather than on its provider.
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [
                _release_candidate(
                    Provider.SUBSOURCE,
                    "weaker-evidence",
                    "Amadeus.1985.480p.DVDRip.xvid-OTHER",
                )
            ],
        ),
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [
                _release_candidate(
                    Provider.SUBDL,
                    "stronger-evidence",
                    "Amadeus.1984.2160p.BluRay.x264-GROUP",
                )
            ],
        ),
    }

    result = SearchCoordinator(adapters).all_providers(_media_request(media))

    assert result.candidates[0].provider_id == "stronger-evidence"
    assert PROVIDER_RELIABILITY[Provider.SUBDL] < PROVIDER_RELIABILITY[
        Provider.SUBSOURCE
    ]


def test_provider_reliability_breaks_a_tie_on_equal_compatibility():
    # Same release name, so the evidence is identical and only the tiebreak can
    # decide: the more reliable provider takes the earlier row.
    release = "Amadeus.1984.2160p.BluRay.x264-GROUP"
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_release_candidate(Provider.SUBDL, "low", release)],
        ),
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [_release_candidate(Provider.SUBSOURCE, "high", release)],
        ),
    }

    result = SearchCoordinator(adapters).all_providers(_media_request("Amadeus (1984)"))

    assert _compatibilities(result)[0] == _compatibilities(result)[1]
    assert result.candidates[0].provider is Provider.SUBSOURCE


def test_compatibility_uses_the_media_file_name_when_the_query_is_weaker():
    # The user may have typed a search term that matches nothing in the release
    # name. The ranking still has to reflect the file on disk.
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [
                _release_candidate(
                    Provider.SUBDL, "right", "Amadeus.1984.1080p.BluRay.x264-G"
                ),
                _release_candidate(
                    Provider.SUBDL, "wrong", "Amelie.2001.1080p.BluRay.x264-G"
                ),
            ],
        )
    }
    request = SearchRequest(
        media_path="Amadeus (1984) 1080p BluRay x264-G.mkv",
        query="zzz no match zzz",
        language="ar",
    )

    result = SearchCoordinator(adapters).all_providers(request)

    assert result.candidates[0].provider_id == "right"
    assert result.candidates[0].compatibility > result.candidates[1].compatibility


def test_a_candidate_the_engine_never_saw_reports_no_measurement():
    # With no media name to compare against there is no evidence, and the panes
    # must say so rather than claim a confident 0%.
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL, [_release_candidate(Provider.SUBDL, "1", "Amadeus")]
        )
    }
    request = SearchRequest(media_path="", query="", language="ar")

    result = SearchCoordinator(adapters).all_providers(request)

    assert result.candidates[0].compatibility == 0
    assert result.candidates[0].compatibility_evidence == ()


def test_an_unavailable_compatibility_engine_still_ranks_a_hash_match_first():
    # library/ is optional at runtime: the TUI has to stay usable when its import
    # fails. Every candidate then reports no measurement, so the hash flag is the
    # only evidence left and must still sort above a filename-only match.
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [
                _release_candidate(Provider.SUBDL, "by-name", "Amadeus.1984"),
                _release_candidate(
                    Provider.SUBDL, "by-hash", "Amadeus.1984", hash_match=True
                ),
            ],
        )
    }
    coordinator = SearchCoordinator(adapters)
    coordinator.compatibility_engine = None

    result = coordinator.all_providers(_media_request("Amadeus (1984) 2160p BluRay"))

    assert result.candidates[0].provider_id == "by-hash"
    assert [item.compatibility for item in result.candidates] == [0, 0]
    assert all(item.compatibility_evidence == () for item in result.candidates)


def test_match_reasons_include_the_provider_reason():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }

    result = SearchCoordinator(adapters, scorer=FixedScorer()).all_providers(
        _request("ar")
    )

    assert "provider: SubDL (reliability 1/3)" in result.candidates[0].match_reasons


def test_match_reasons_place_provider_after_the_episode_determination():
    adapters = {
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [_candidate(Provider.SUBSOURCE, "1", "ar")],
        )
    }
    scorer = FixedScorer(
        reasons=("movie/episode match", "season mismatch", "year match 2026")
    )

    result = SearchCoordinator(adapters, scorer=scorer).all_providers(_request("ar"))
    reasons = result.candidates[0].match_reasons

    assert reasons[0] == "movie/episode match"
    assert reasons[2] == "provider: SubSource (reliability 3/3)"


def test_match_reasons_never_exceed_the_cap():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }
    scorer = FixedScorer(
        reasons=(
            "movie/episode match",
            "season mismatch",
            "year match 2026",
            "release name match",
            "source: WEB-DL",
            "resolution: 1080p",
        )
    )

    result = SearchCoordinator(adapters, scorer=scorer).all_providers(_request("ar"))

    assert len(result.candidates[0].match_reasons) == MAX_MATCH_REASONS
    assert "resolution: 1080p" not in result.candidates[0].match_reasons


def test_scorer_without_explain_subtitle_match_scores_with_empty_reasons():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }

    result = SearchCoordinator(adapters, scorer=LegacyScorer()).all_providers(
        _request("ar")
    )

    assert result.candidates[0].score == 42.0
    assert result.candidates[0].match_reasons == ()


def test_search_module_and_library_agree_on_the_reason_cap():
    from library.subtitle_utils import MAX_MATCH_REASONS as library_cap

    assert MAX_MATCH_REASONS == library_cap


# --- English fallback -----------------------------------------------------


class LanguageAwareAdapter(FakeAdapter):
    """Returns only the candidates whose language the request actually asked for.

    FakeAdapter answers every request with the same list, so it cannot distinguish a
    primary search from a fallback search. These tests are about that distinction.
    """

    def search(self, request):
        self.calls += 1
        language = (request.language or "").strip().lower()
        return ProviderSearchResult(
            provider=self.provider,
            candidates=[
                item
                for item in self.candidates
                if (item.language or "").strip().lower() == language
            ],
            error=self.error,
        )


def test_fallback_does_not_run_when_the_target_language_returns_candidates():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language="en",
    )

    assert result.used_fallback is False
    assert adapters[Provider.SUBDL].calls == 1


def test_fallback_runs_and_is_marked_when_the_target_language_is_empty():
    adapters = {
        Provider.SUBDL: LanguageAwareAdapter(Provider.SUBDL),
        Provider.OPENSUBTITLES: LanguageAwareAdapter(
            Provider.OPENSUBTITLES,
            [_candidate(Provider.OPENSUBTITLES, "en", "en")],
        ),
    }
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language="en",
    )

    assert result.used_fallback is True
    assert result.fallback_language == "en"
    assert [item.language for item in result.candidates] == ["en"]


def test_fallback_does_not_run_when_it_equals_the_target_language():
    adapters = {Provider.SUBDL: FakeAdapter(Provider.SUBDL)}
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language="ar",
    )

    assert result.used_fallback is False
    assert adapters[Provider.SUBDL].calls == 1


@pytest.mark.parametrize("fallback_language", ["", "   "])
def test_fallback_does_not_run_when_disabled(fallback_language):
    adapters = {Provider.SUBDL: FakeAdapter(Provider.SUBDL)}
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language=fallback_language,
    )

    assert result.used_fallback is False
    assert adapters[Provider.SUBDL].calls == 1


def test_a_fallback_search_error_leaves_the_primary_result_intact():
    adapters = {Provider.SUBDL: FakeAdapter(Provider.SUBDL, error="target down")}
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    def fail_on_the_fallback(request):
        if (request.language or "").strip().lower() != "ar":
            raise RuntimeError("fallback exploded")
        return coordinator.all_providers(request)

    result = coordinator.search_with_fallback(
        _request("ar"),
        fail_on_the_fallback,
        fallback_language="en",
    )

    assert result.candidates == []
    assert result.errors[Provider.SUBDL] == "target down"
    assert result.used_fallback is False


def test_fallback_uses_the_injected_mode_entry_point():
    adapters = {
        Provider.SUBSOURCE: LanguageAwareAdapter(
            Provider.SUBSOURCE,
            [_candidate(Provider.SUBSOURCE, "en", "en")],
        )
    }
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.auto,
        fallback_language="en",
    )

    # selected_provider is only ever set by auto(), so a marked fallback result that
    # carries it can only have come from the injected entry point.
    assert result.used_fallback is True
    assert result.selected_provider is Provider.SUBSOURCE
