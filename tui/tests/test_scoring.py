import pytest

from library.subtitle_utils import SubtitleUtils, fold_arabic


@pytest.fixture
def scorer():
    return SubtitleUtils()


PITT_QUERY = (
    "The Pitt (2025) - S01E01 - - 7-00 A.M "
    "[AMZN Flux WEBDL-1080p Proper][8bit][h264][EAC3 Atmos 5.1]-FLUX"
)
UNRELATED_S01E01 = (
    "The.World.of.the.Married.S01E01.720p.WEB-DL.x264-Pahe.in"
)
WIDOWS_BAY_QUERY = (
    "Widow's Bay (2026) - S01E01 - - Welcome to Widows Bay! "
    "[ATVP WEBDL-2160p][10bit][h265][EAC3 Atmos 5.1]-Friday4KPopcorn"
)


def test_same_series_conflicts_outrank_unrelated_exact_episode(scorer):
    exact = scorer.score_subtitle(
        "The.Pitt.S01E01.1080p.WEB.H264-SuccessfulCrab",
        PITT_QUERY,
    )
    wrong_season = scorer.score_subtitle(
        "The.Pitt.S02E01.1080p.WEB.h264-ETHEL.ar",
        PITT_QUERY,
    )
    wrong_episode = scorer.score_subtitle(
        "The.Pitt.S01E02.1080p.WEB.H264-SuccessfulCrab",
        PITT_QUERY,
    )
    unrelated = scorer.score_subtitle(UNRELATED_S01E01, PITT_QUERY)

    assert exact > wrong_season > unrelated
    assert exact > wrong_episode > unrelated


def test_same_series_with_unknown_episode_outranks_unrelated_exact_episode(scorer):
    same_series_unknown_episode = scorer.score_subtitle(
        "The.Pitt.Arabic.WEB-DL",
        PITT_QUERY,
    )
    unrelated = scorer.score_subtitle(UNRELATED_S01E01, PITT_QUERY)

    assert same_series_unknown_episode > unrelated


@pytest.mark.parametrize(
    "matching_release",
    [
        "Hajime no Ippo – 42 [ALOIN][DVD]",
        "Hajime no Ippo Round 42 [DVD]",
        "Hajime no Ippo 42 END [DVD]",
    ],
)
def test_labeled_or_bare_episode_adds_a_bounded_nudge(scorer, matching_release):
    query = "Hajime no Ippo S01E42 1080p"
    matching = scorer.score_subtitle(matching_release, query)
    unknown = scorer.score_subtitle("Hajime no Ippo Special [DVD]", query)

    assert matching >= unknown + 5


def test_bare_episode_does_not_treat_year_or_resolution_as_episode(scorer):
    query = "Movie S01E24"
    neutral = scorer.score_subtitle("Movie Special", query)
    year = scorer.score_subtitle("Movie - 2024", query)
    resolution = scorer.score_subtitle("Movie - 24 1080p", query)

    assert year <= neutral + 2
    assert resolution <= neutral + 2


def test_one_shared_generic_title_token_does_not_establish_same_series(scorer):
    misleading = scorer.score_subtitle(
        "World Trigger S01E01",
        "World News S01E01",
    )
    same_title_unknown_episode = scorer.score_subtitle(
        "World News Special",
        "World News S01E01",
    )

    assert same_title_unknown_episode > misleading


def test_apostrophe_title_matches_apostrophe_free_release(scorer):
    # Release naming drops apostrophes ("Widow's Bay" -> "Widows Bay"), so a
    # video whose title carries one must still match the scene-named release
    # as the same series.
    same_series = scorer.score_subtitle(
        "Widows.Bay.2026.S01E01.ATVP.WEB-DL.2160p.HDR.H.265",
        WIDOWS_BAY_QUERY,
    )
    unrelated = scorer.score_subtitle(UNRELATED_S01E01, WIDOWS_BAY_QUERY)

    assert same_series >= 90
    assert same_series > unrelated


# --- Arabic folding -------------------------------------------------------

def test_fold_arabic_removes_harakat():
    # U+064F damma, U+064E fatha, U+0652 sukun
    marked = "م" + "ُ" + "س" + "َ" + "ل" + "ْ" + "س" + "ُ" + "ل"
    assert fold_arabic(marked) == "مسلسل"


def test_fold_arabic_removes_superscript_alef_and_quranic_marks():
    # U+0670 superscript alef, U+06D6 Quranic annotation sign
    marked = "م" + "ٰ" + "س" + "ۖ" + "ل"
    assert fold_arabic(marked) == "مسل"


def test_fold_arabic_removes_tatweel():
    # U+0640 tatweel, the Arabic letter elongation character
    assert fold_arabic("م" + "ـ" + "ـ" + "سلسل") == "مسلسل"


def test_fold_arabic_unifies_alef_forms():
    # U+0622 madda, U+0623 hamza above, U+0625 hamza below, U+0671 wasla
    assert fold_arabic("آأإٱ") == "اااا"


def test_fold_arabic_unifies_teh_marbuta_to_heh():
    assert fold_arabic("مدرسة") == (
        "مدرسه"
    )


def test_fold_arabic_unifies_alef_maksura_to_yeh():
    assert fold_arabic("موسى") == "موسي"


def test_fold_arabic_maps_arabic_indic_digits_to_ascii():
    assert fold_arabic("١٢٣") == "123"


def test_fold_arabic_maps_extended_arabic_indic_digits_to_ascii():
    assert fold_arabic("۱۲۳") == "123"


def test_fold_arabic_leaves_hamza_letters_alone():
    # U+0624 waw with hamza, U+0626 yeh with hamza: distinct letters.
    assert fold_arabic("ؤئ") == "ؤئ"


def test_fold_arabic_is_total():
    assert fold_arabic(None) is None
    assert fold_arabic("") == ""
    assert fold_arabic("The Pitt S01E01") == "The Pitt S01E01"


def test_fold_arabic_is_idempotent():
    once = fold_arabic("أَمـى")
    assert fold_arabic(once) == once


# --- Arabic survives match normalization (Amendment 1 regression) ---------

def test_arabic_title_survives_match_normalization(scorer):
    # Regression: the "drop everything else" pass used to erase Arabic, so every
    # Arabic release scored as an unknown title and was capped at 15/100.
    # Note the retained title is the FOLDED spelling: the trailing character is heh
    # (U+0647), not the teh marbuta (U+0629) the input carried.
    assert scorer._normalize_match_text("الهيبة.S01E03.1080p.WEB-DL") == (
        "الهيبه s01e03 1080p web dl"
    )


def test_latin_text_normalization_is_unchanged(scorer):
    assert scorer._normalize_match_text("The.Pitt.S01E01.1080p.WEB-DL") == (
        "the pitt s01e01 1080p web dl"
    )


def test_arabic_title_yields_a_hypothesis(scorer):
    # The precondition for Arabic matching: hypotheses must be non-empty and must
    # carry the Arabic title through. Asserted as a property rather than an exact
    # list, because the hypothesis text is an internal formatting detail. The title
    # is compared in its folded spelling, which is what matching operates on.
    hypotheses = scorer._title_hypotheses("الهيبة.S01E03.1080p.WEB-DL")
    folded_title = fold_arabic("الهيبة")

    assert hypotheses
    assert any(folded_title in hypothesis for hypothesis in hypotheses)


def test_diacritized_and_plain_arabic_titles_score_equally(scorer):
    plain = "الهيبة S01E03 1080p"
    # kasra on heh, fatha on beh -- folded away before comparison
    marked = "ال" + "ه" + "ِ" + "ي" + "ب" + "َ" + "ة S01E03 1080p"

    assert scorer.score_subtitle(marked, plain) == scorer.score_subtitle(plain, plain)


def test_arabic_same_series_outranks_arabic_other_series(scorer):
    query = "الهيبة S01E03 1080p"
    same_series = scorer.score_subtitle("الهيبة.S01E03.1080p.WEB-DL", query)
    other_series = scorer.score_subtitle("باب_الحارة.S01E03.1080p.WEB-DL", query)

    assert same_series > other_series
