"""Tests for subtitle list sorting resilience."""

from library.subtitle_utils import SubtitleUtils


def test_sort_list_of_dicts_keeps_malformed_rows():
    rows = [
        {"id": 1, "attributes": {}},
        {"id": 2, "attributes": {"download_count": 10}},
        {"id": 3, "attributes": {"download_count": 5}},
    ]

    result = SubtitleUtils().sort_list_of_dicts_by_key(rows, "download_count")

    assert [r["id"] for r in result] == [2, 3, 1]


def test_sort_subtitle_list_keeps_malformed_rows():
    rows = [
        {"id": 1, "attributes": {}},
        {"id": 2, "attributes": {"download_count": 10}},
        {"id": 3, "attributes": {"download_count": 5}},
    ]

    result = SubtitleUtils().sort_subtitle_list(rows)

    assert [r["id"] for r in result] == [2, 3, 1]


def test_sort_subtitle_list_with_custom_scores():
    rows = [
        {"id": 1, "attributes": {"download_count": 100}},
        {"id": 2, "attributes": {"download_count": 50}},
        {"id": 3, "attributes": {"download_count": 200}},
    ]
    scores = {1: 10, 2: 30, 3: 5}

    result = SubtitleUtils().sort_subtitle_list(rows, scores=scores)

    assert [r["id"] for r in result] == [2, 1, 3]


def test_sort_subtitle_list_missing_id_uses_zero_score():
    rows = [
        {"attributes": {"download_count": 100}},
        {"id": 2, "attributes": {"download_count": 50}},
    ]
    scores = {2: 10}

    result = SubtitleUtils().sort_subtitle_list(rows, scores=scores)

    assert [r.get("id") for r in result] == [2, None]


def test_sort_list_of_dicts_dedupes_by_id():
    rows = [
        {"id": 1, "attributes": {"download_count": 10}},
        {"id": 1, "attributes": {"download_count": 5}},
        {"id": 2, "attributes": {"download_count": 3}},
    ]

    result = SubtitleUtils().sort_list_of_dicts_by_key(rows, "download_count")

    assert [r["id"] for r in result] == [1, 2]
