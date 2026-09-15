"""Compatibility between a subtitle release and the media file it is for.

The percentage is *verified compatibility evidence*, not a probability, so these
tests pin three properties above all: a facet the evidence confirms earns its
points, a facet the evidence cannot establish earns none without being reported
as a mismatch, and a facet the evidence contradicts is punished hard enough to
move the result into a lower band.
"""

from __future__ import annotations

import pytest

from library.compatibility import (
    compatibility,
    compatibility_badge,
    media_evidence_name,
    parse_release_facets,
)

MOVIE_MEDIA = "Amadeus (1984) 1080p BluRay x264-GROUP"
EPISODE_MEDIA = "The.Pitt.S01E03.1080p.WEB-DL.x264-GROUP"


# --- Reading the evidence out of a release name ---------------------------


def test_a_release_name_yields_every_facet_it_states():
    facets = parse_release_facets("Amadeus (1984) Directors Cut 2160p BluRay x265-GROUP")

    assert facets.title == "amadeus"
    assert facets.year == 1984
    assert facets.edition == "Director's Cut"
    assert facets.source == "BluRay"
    assert facets.resolution == "2160p"
    assert facets.codec == "H.265"
    assert facets.group == "GROUP"


def test_a_hyphenated_source_is_not_mistaken_for_a_release_group():
    # "WEB-DL" ends in a hyphenated token; reading the text after the last hyphen
    # as a release group would file the source under the wrong facet and leave the
    # source unknown.
    facets = parse_release_facets("Amadeus.1984.1080p.WEB-DL")

    assert facets.source == "WEB-DL"
    assert facets.group is None


def test_a_release_group_is_read_after_the_last_hyphen():
    assert parse_release_facets(MOVIE_MEDIA).group == "GROUP"


def test_a_file_extension_never_becomes_part_of_the_title():
    assert parse_release_facets("Amadeus (1984).mkv").title == "amadeus"
    assert parse_release_facets("Amadeus (1984).srt").title == "amadeus"


def test_episode_and_season_are_read_from_the_release_name():
    facets = parse_release_facets(EPISODE_MEDIA)

    assert facets.season == 1
    assert facets.episode == 3


def test_a_season_pack_states_a_season_and_no_episode():
    # A season pack is a complete name, not a silent one. Read as having neither
    # a season nor an episode it would be indistinguishable from a film, which
    # changes both its own denominator and whether an episode subtitle conflicts.
    facets = parse_release_facets("The.Pitt.S01.1080p.WEB-DL.x264-GROUP")

    assert facets.season == 1
    assert facets.episode is None
    # "s01" is the season, so it is not part of the title.
    assert facets.title == "the pitt"


def test_a_title_that_merely_contains_a_digit_is_left_alone():
    # The guard on reading a bare season: only a whole "s<digits>" word counts, so
    # a title is not shortened by a stray match inside it.
    facets = parse_release_facets("S1m0ne.2002.1080p.BluRay.x264-GROUP")

    assert facets.season is None
    assert "s1m0ne" in facets.title


def test_a_release_states_no_edition_until_it_states_one():
    # The whole point of the feature: Amadeus (1984).mkv does not say whether it
    # is the theatrical cut, so nothing may claim it is.
    assert parse_release_facets("Amadeus (1984) 1080p BluRay").edition is None


def test_four_k_is_the_same_resolution_as_2160p():
    assert parse_release_facets("Amadeus.1984.4K.HDR").resolution == "2160p"


def test_arabic_orthographic_variants_compare_as_one_title():
    folded = parse_release_facets("الهيبة (2017) 1080p BluRay").title
    variant = parse_release_facets("الهيبه (2017) 1080p BluRay").title

    assert folded == variant


# --- The evidence has to earn its points ----------------------------------


def test_an_exact_file_hash_is_the_strongest_evidence_there_is():
    identical_file_name = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)
    hashed = compatibility(MOVIE_MEDIA, MOVIE_MEDIA, hash_match=True)

    assert hashed.percent == 100
    assert hashed.badge == "BEST"
    assert identical_file_name.percent < hashed.percent


def test_verifying_every_named_facet_still_does_not_match_a_verified_file():
    fullest_file_name = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)

    assert fullest_file_name.percent <= 90
    assert (
        fullest_file_name.percent
        < compatibility(MOVIE_MEDIA, MOVIE_MEDIA, hash_match=True).percent
    )


def test_a_release_with_more_verified_facets_scores_higher():
    # The percentage counts verified evidence, so each additional facet the
    # release actually states has to move it.
    bare = compatibility("Amadeus (1984)", MOVIE_MEDIA)
    sourced = compatibility("Amadeus (1984) BluRay", MOVIE_MEDIA)
    full = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)

    assert bare.percent < sourced.percent < full.percent


def test_an_exact_title_and_year_outrank_the_title_alone():
    with_year = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)
    without_year = compatibility("Amadeus 1080p BluRay x264-GROUP", MOVIE_MEDIA)

    assert with_year.percent > without_year.percent


def test_a_partial_title_outranks_an_unrelated_one():
    partial = compatibility("Amadeus and Salieri (1984) 1080p BluRay", MOVIE_MEDIA)
    unrelated = compatibility("Salieri (1984) 1080p BluRay", MOVIE_MEDIA)

    assert partial.percent > unrelated.percent
    assert unrelated.badge == "MISMATCH"


def test_the_right_episode_outranks_a_wrong_episode():
    right = compatibility(EPISODE_MEDIA, EPISODE_MEDIA)
    wrong = compatibility("The.Pitt.S01E04.1080p.WEB-DL.x264-GROUP", EPISODE_MEDIA)

    assert right.percent > wrong.percent
    assert wrong.badge == "MISMATCH"
    assert "media: E03" in wrong.conflicts
    assert "subtitle: E04" in wrong.conflicts


def test_the_right_episode_in_the_wrong_season_is_a_mismatch():
    wrong = compatibility("The.Pitt.S02E03.1080p.WEB-DL.x264-GROUP", EPISODE_MEDIA)

    assert wrong.badge == "MISMATCH"
    assert "media: S01" in wrong.conflicts
    assert "subtitle: S02" in wrong.conflicts


def test_a_film_does_not_match_a_subtitle_that_claims_an_episode():
    # The bug this closes: a media file with no season or episode is a film, so a
    # subtitle declaring S01E03 is for something else entirely. Treating the
    # film's silence as "unknown" let it score identically to the real release.
    exact = compatibility("Amadeus.1984.1080p.BluRay.x264-GROUP", MOVIE_MEDIA)
    claims_an_episode = compatibility(
        "Amadeus.1984.S01E03.1080p.BluRay.x264-GROUP", MOVIE_MEDIA
    )

    assert claims_an_episode.badge == "MISMATCH"
    assert claims_an_episode.percent < exact.percent
    assert claims_an_episode.percent <= 34
    assert "media: film" in claims_an_episode.conflicts
    assert "subtitle: S01E03" in claims_an_episode.conflicts
    # A conflict replaces the evidence block: a reader must not see the pluses
    # next to a subtitle that is for the wrong thing.
    assert claims_an_episode.evidence_lines()[0] == "Conflict:"


@pytest.mark.parametrize(
    "release, expected",
    [
        ("Amadeus.1984.S01E03.1080p.BluRay.x264-GROUP", "S01E03"),
        ("Amadeus.1984.S01.1080p.BluRay.x264-GROUP", "S01"),
        ("Amadeus.1984.E03.1080p.BluRay.x264-GROUP", "E03"),
    ],
)
def test_every_way_a_film_subtitle_can_claim_an_episode_conflicts(release, expected):
    # A season pack, a lone episode and a full SxxExx all say the subtitle is for
    # an episode, and a film has none of them.
    match = compatibility(release, MOVIE_MEDIA)

    assert match.badge == "MISMATCH"
    assert f"subtitle: {expected}" in match.conflicts


def test_a_film_subtitle_that_claims_nothing_keeps_its_score():
    # The guard on the rule above: only a *claim* conflicts, so a film subtitle
    # that stays silent about season and episode is scored exactly as before.
    silent = compatibility("Amadeus.1984.1080p.BluRay.x264-GROUP", MOVIE_MEDIA)

    assert silent.conflicts == ()
    assert silent.badge == "BEST"


def test_an_episode_inside_a_season_pack_is_not_a_conflict():
    # A season pack's media name states a season but no episode, so an episode
    # subtitle for it is compatible -- the pack contains that episode. Only the
    # film case, which has neither, is a conflict.
    season_pack = "The.Pitt.S01.1080p.WEB-DL.x264-GROUP"
    episode = compatibility("The.Pitt.S01E03.1080p.WEB-DL.x264-GROUP", season_pack)

    assert episode.conflicts == ()
    assert episode.badge != "MISMATCH"


def test_a_film_conflict_never_outranks_the_exact_release():
    # The user-facing consequence: the row for the real movie release has to sit
    # above a row whose subtitle is for some television episode.
    results = {
        "exact": compatibility("Amadeus.1984.1080p.BluRay.x264-GROUP", MOVIE_MEDIA),
        "episode": compatibility(
            "Amadeus.1984.S01E03.1080p.BluRay.x264-GROUP", MOVIE_MEDIA
        ),
    }

    assert results["exact"].percent > results["episode"].percent


def test_a_matching_edition_outranks_a_directors_cut_mismatch():
    media = "Amadeus (1984) Theatrical 1080p BluRay x264-GROUP"
    theatrical = compatibility(media, media)
    directors_cut = compatibility(
        "Amadeus (1984) Directors Cut 1080p BluRay x264-GROUP", media
    )

    assert theatrical.percent > directors_cut.percent
    assert directors_cut.badge == "MISMATCH"
    assert "media: Theatrical" in directors_cut.conflicts
    assert "subtitle: Director's Cut" in directors_cut.conflicts
    # "Strongly reduce" has to mean a whole band, not a nudge.
    assert theatrical.percent - directors_cut.percent >= 25


def test_a_matching_source_outranks_a_conflicting_one():
    bluray = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)
    web_dl = compatibility("Amadeus (1984) 1080p WEB-DL x264-GROUP", MOVIE_MEDIA)

    assert bluray.percent > web_dl.percent
    assert web_dl.badge != "MISMATCH"


def test_a_matching_resolution_is_only_a_small_gain():
    with_resolution = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)
    without_resolution = compatibility("Amadeus (1984) BluRay x264-GROUP", MOVIE_MEDIA)

    gain = with_resolution.percent - without_resolution.percent
    assert 0 < gain <= 10


def test_a_codec_conflict_never_outweighs_a_source_conflict():
    codec_conflict = compatibility(
        "Amadeus (1984) 1080p BluRay x265-GROUP", MOVIE_MEDIA
    )
    source_conflict = compatibility(
        "Amadeus (1984) 1080p WEB-DL x264-GROUP", MOVIE_MEDIA
    )

    assert codec_conflict.percent > source_conflict.percent


# --- Unknown is not a mismatch --------------------------------------------


def test_sparse_evidence_never_reaches_the_top_band():
    # "Amadeus (1984).mkv" verifies a title and a year and nothing else. Reading
    # only the facets that happen to be present would make that 100% BEST, which
    # would claim a compatibility nobody established.
    sparse = compatibility("Amadeus (1984).mkv", "Amadeus (1984).mkv")

    assert sparse.badge != "BEST"
    assert sparse.percent < 90


def test_unknown_facets_are_reported_as_unknown_rather_than_as_conflicts():
    sparse = compatibility("Amadeus (1984).mkv", "Amadeus (1984).mkv")

    assert sparse.conflicts == ()
    assert sparse.evidence_lines() == (
        "Evidence:",
        "+ exact title",
        "+ year 1984",
        "- edition unknown",
        "- no hash match",
    )


def test_a_facet_the_media_never_states_earns_nothing_and_costs_nothing():
    media = "Amadeus (1984) 1080p BluRay x264-GROUP"
    silent = compatibility("Amadeus (1984) 1080p BluRay x264-GROUP", media)
    claiming = compatibility(
        "Amadeus (1984) Directors Cut 1080p BluRay x264-GROUP", media
    )

    # The media states no edition, so an edition claim has been neither confirmed
    # nor contradicted: it earns nothing and costs nothing, exactly like silence.
    # Penalising it would punish a subtitle for the media's silence.
    assert claiming.conflicts == ()
    assert claiming.percent == silent.percent
    assert "- edition unverified" in claiming.evidence_lines()


def test_every_known_match_is_listed_with_a_plus_and_every_unknown_with_a_minus():
    match = compatibility(MOVIE_MEDIA, MOVIE_MEDIA)
    lines = match.evidence_lines()

    assert lines[0] == "Evidence:"
    assert "+ exact title" in lines
    assert "+ year 1984" in lines
    assert "+ BluRay" in lines
    assert "+ 1080p" in lines
    assert "- edition unknown" in lines
    assert "- no hash match" in lines
    assert all(line.startswith(("+", "-")) for line in lines[1:])


def test_a_hash_match_explains_itself_with_only_the_hash():
    lines = compatibility(MOVIE_MEDIA, MOVIE_MEDIA, hash_match=True).evidence_lines()

    assert lines == ("Evidence:", "+ exact file hash")


def test_a_conflict_replaces_the_evidence_block():
    media = "Amadeus (1984) Theatrical 1080p BluRay"
    conflict = compatibility("Amadeus (1984) Directors Cut 1080p BluRay", media)

    assert conflict.evidence_lines() == (
        "Conflict:",
        "media: Theatrical",
        "subtitle: Director's Cut",
    )


@pytest.mark.parametrize(
    "release",
    [
        "",
        "   ",
        "1080p BluRay",
        MOVIE_MEDIA,
        "Amadeus (1984) Theatrical Extended Unrated Remastered Criterion 2160p",
        "فيلم عربي (2019) 1080p WEB-DL",
        "!!! ???",
        "Salieri (1985) 720p HDTV x265-OTHER",
    ],
)
def test_the_percentage_always_stays_between_zero_and_one_hundred(release):
    percent = compatibility(release, MOVIE_MEDIA).percent

    assert isinstance(percent, int)
    assert 0 <= percent <= 100


def test_an_empty_release_verifies_nothing():
    match = compatibility("", MOVIE_MEDIA)

    assert match.percent == 0
    assert match.badge == "MISMATCH"
    assert match.evidence_lines() == ()


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        (100, "BEST"),
        (90, "BEST"),
        (89, "GREAT"),
        (75, "GREAT"),
        (74, "GOOD"),
        (55, "GOOD"),
        (54, "MAYBE"),
        (35, "MAYBE"),
        (34, "MISMATCH"),
        (0, "MISMATCH"),
    ],
)
def test_the_badge_names_the_band_the_percentage_falls_in(percent, expected):
    assert compatibility_badge(percent) == expected


def test_the_badge_bands_cover_every_possible_percentage():
    for percent in range(0, 101):
        assert compatibility_badge(percent) in {
            "BEST",
            "GREAT",
            "GOOD",
            "MAYBE",
            "MISMATCH",
        }


# --- Choosing what to compare the release against -------------------------


def test_the_media_file_name_is_the_evidence_of_first_resort(tmp_path):
    media = tmp_path / "Amadeus (1984) 1080p BluRay x264-GROUP.mkv"
    media.touch()

    assert media_evidence_name(str(media)) == "Amadeus (1984) 1080p BluRay x264-GROUP"


def test_a_generically_named_file_falls_back_to_its_folder(tmp_path):
    folder = tmp_path / "Amadeus (1984) 1080p BluRay"
    folder.mkdir()
    media = folder / "movie.mkv"
    media.touch()

    assert media_evidence_name(str(media)) == "Amadeus (1984) 1080p BluRay"


def test_a_richer_file_name_is_not_displaced_by_its_folder(tmp_path):
    folder = tmp_path / "Movies"
    folder.mkdir()
    media = folder / "Amadeus (1984) 1080p BluRay.mkv"
    media.touch()

    assert media_evidence_name(str(media)) == "Amadeus (1984) 1080p BluRay"


def test_a_path_with_nothing_to_compare_against_yields_no_evidence(tmp_path):
    folder = tmp_path / "Movies"
    folder.mkdir()
    media = folder / "movie.mkv"
    media.touch()

    assert media_evidence_name(str(media)) == ""


def test_no_media_path_yields_no_evidence():
    assert media_evidence_name("") == ""
