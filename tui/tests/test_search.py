import threading

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
