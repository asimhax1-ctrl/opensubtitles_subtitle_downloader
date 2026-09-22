"""Tests for release-name parsing: season/episode, year, edition, codec."""

import pytest

from library.compatibility import _episode_of, _year_of, parse_release_facets
from library.subtitle_utils import SubtitleUtils


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("The.Pitt.S01E02", (1, 2)),
        ("The.Pitt.S02.E03", (2, 3)),
        ("Show.S10.E05", (10, 5)),
        ("Show.S02.EP003", (2, 3)),
        ("The.Pitt.S01E102", (1, 102)),
        ("Movie.1920x1080", (None, None)),
        ("Show.2024.01.02", (None, None)),
        ("Show.2024-01-02", (None, None)),
        ("Show.S01.1080p", (1, None)),
    ],
)
def test_extract_season_and_episode_matches_shared_parser(name, expected):
    """The provider matcher and the scoring parser must never disagree."""
    assert SubtitleUtils().extract_season_and_episode(name) == expected
    assert _episode_of(name) == expected


def test_extract_season_and_episode_delegates_to_shared_evidence():
    """The public helper is a thin wrapper around the same parser ranking uses."""
    name = "The.Pitt.S02.E03.1080p.WEB-DL"
    from_scoring = SubtitleUtils()._episode_evidence(name, allow_bare=True)[:2]
    from_helper = SubtitleUtils().extract_season_and_episode(name)
    assert from_helper == from_scoring


@pytest.mark.parametrize(
    ("name", "expected_year", "expected_title_contains"),
    [
        ("2012.2009.1080p.BluRay", "2009", "2012"),
        ("2001.A.Space.Odyssey.1968", "1968", "2001"),
        ("The.Pitt.2024.S01E01", "2024", "pitt"),
    ],
)
def test_year_parsing_prefers_release_context(
    name, expected_year, expected_title_contains
):
    assert _year_of(name) == expected_year
    facets = parse_release_facets(name)
    assert expected_title_contains in facets.title


def test_edition_abbreviations_require_release_context():
    facets = parse_release_facets("DC.League.of.Super-Pets.2022.1080p.WEB-DL")
    assert facets.edition is None
    assert "dc" in facets.title

    facets = parse_release_facets("Amadeus.Directors.Cut.1984.1080p")
    assert facets.edition == "Director's Cut"


def test_dotted_codec_is_detected():
    facets = parse_release_facets("Movie.2020.1080p.AAC2.0.H.264-GRP")
    assert facets.codec == "H.264"
    assert "aac" not in facets.title


def test_audio_line_tokens_removed_without_harming_real_titles():
    facets = parse_release_facets("Movie.2020.1080p.WEB-DL.DDP5.1.x264-GRP")
    assert "ddp" not in facets.title
    assert "5" not in facets.title.split()

    # Short tokens like "Ma", "It", "H" must survive as title words.
    assert parse_release_facets("Ma.2019.1080p").title == "ma"
    assert parse_release_facets("It.2017.1080p").title == "it"
