from pathlib import Path

import pytest

from tui.auto_quality import (
    FACTOR_WEIGHTS,
    FactorScore,
    QualityReport,
    choose_candidate,
    evaluate_subtitle,
)


def write_subtitle(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def arabic_srt(cues: int = 8) -> str:
    blocks = []
    for index in range(cues):
        start = index * 60 + 1
        end = start + 3
        blocks.append(
            f"{index + 1}\n00:{start // 60:02}:{start % 60:02},000 --> "
            f"00:{end // 60:02}:{end % 60:02},000\n"
            f"هذا سطر ترجمة واضح ومكتمل للاختبار رقم {index}"
        )
    return "\n\n".join(blocks) + "\n"


def test_auto_quality_weights_are_fixed_at_exactly_one_hundred():
    assert list(FACTOR_WEIGHTS.values()) == [20, 20, 20, 15, 15, 10]
    assert sum(FACTOR_WEIGHTS.values()) == 100


def test_missing_media_duration_does_not_award_full_completeness(tmp_path):
    subtitle = write_subtitle(tmp_path, "candidate.srt", arabic_srt())

    report = evaluate_subtitle(subtitle, requested_language="ar")

    assert report.accepted
    completeness = report.factor("completeness")
    assert completeness.points == 0
    assert completeness.known is False
    assert report.score <= 80
    assert report.confidence < 1.0


def test_known_duration_scores_span_instead_of_assuming_a_cue_count(tmp_path):
    longer = write_subtitle(tmp_path, "longer.srt", arabic_srt(8))
    shorter = write_subtitle(tmp_path, "shorter.srt", arabic_srt(2))

    long_report = evaluate_subtitle(
        longer,
        requested_language="ar",
        media_duration=480,
    )
    short_report = evaluate_subtitle(
        shorter,
        requested_language="ar",
        media_duration=480,
    )

    assert long_report.factor("completeness").points == 20
    assert short_report.factor("completeness").points < 5


def test_wrong_language_is_a_hard_rejection(tmp_path):
    subtitle = write_subtitle(
        tmp_path,
        "wrong.srt",
        "1\n00:00:01,000 --> 00:00:03,000\nThis is an English sentence.\n",
    )

    report = evaluate_subtitle(
        subtitle,
        requested_language="ar",
        media_duration=120,
    )

    assert not report.accepted
    assert report.hard_rejection == "wrong_language"


def test_zero_valid_timed_cues_are_a_hard_rejection(tmp_path):
    subtitle = write_subtitle(
        tmp_path,
        "untimed.srt",
        "Just Arabic text بلا توقيت\n",
    )

    report = evaluate_subtitle(subtitle, requested_language="ar")

    assert not report.accepted
    assert report.hard_rejection == "zero_valid_timed_cues"


def test_ass_override_tags_are_ignored_for_scoring_but_file_is_unchanged(tmp_path):
    original = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "0,0,1,1,0,2,10,10,10,1",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text",
            r"Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,{\an8}{\b1}"
            "مرحبا بالعالم\\Nسطر ثانٍ",
        ]
    ) + "\n"
    subtitle = write_subtitle(tmp_path, "styled.ass", original)

    report = evaluate_subtitle(
        subtitle,
        requested_language="ar",
        media_duration=10,
    )

    assert report.accepted
    assert report.factor("language_integrity").points > 0
    assert subtitle.read_text(encoding="utf-8") == original


def test_microdvd_without_fps_uses_frames_and_content_only(tmp_path):
    subtitle = write_subtitle(
        tmp_path,
        "candidate.sub",
        "{25}{75}مرحبا بالعالم\n{76}{120}سطر ترجمة آخر\n",
    )

    report = evaluate_subtitle(subtitle, requested_language="ar")

    assert report.accepted
    assert report.factor("completeness").known is False
    assert report.factor("timing_sanity").known is False
    assert "without FPS" in report.factor("timing_sanity").details[0]
    assert report.factor("structural_validity").points > 0


def test_microdvd_out_of_order_frames_lose_structural_points(tmp_path):
    subtitle = write_subtitle(
        tmp_path,
        "out-of-order.sub",
        "{100}{125}مرحبا بالعالم\n{25}{50}سطر ترجمة آخر\n",
    )

    report = evaluate_subtitle(subtitle, requested_language="ar")

    assert report.factor("structural_validity").points < 20


def _report(
    *,
    structural=20,
    completeness=20,
    readability=20,
    timing=15,
    language=15,
    fit=0,
    fit_known=False,
):
    factors = tuple(
        FactorScore(name, points, FACTOR_WEIGHTS[name], known, ())
        for name, points, known in (
            ("structural_validity", structural, True),
            ("completeness", completeness, True),
            ("readability", readability, True),
            ("timing_sanity", timing, True),
            ("language_integrity", language, True),
            ("release_fit", fit, fit_known),
        )
    )
    score = round(sum(factor.points for factor in factors))
    return QualityReport(score, True, None, factors, 1.0)


def test_close_excellent_candidates_auto_select_without_conflicts():
    winner = _report()
    near_tie = _report(readability=19)

    decision = choose_candidate([winner, near_tie])

    assert decision.chosen_index == 0
    assert decision.manual_required is False


def test_material_quality_tradeoff_falls_back_to_manual_choice():
    readability_first = _report(structural=14, readability=20)
    structure_first = _report(structural=20, readability=14)

    decision = choose_candidate([readability_first, structure_first])

    assert decision.chosen_index is None
    assert decision.manual_required is True


def test_small_gap_below_excellent_quality_falls_back_to_manual_choice():
    first = _report(readability=10)
    second = _report(readability=9)

    decision = choose_candidate([first, second])

    assert decision.chosen_index is None
    assert decision.manual_required is True


def test_fit_cannot_override_a_better_content_score(tmp_path):
    clean = write_subtitle(tmp_path, "clean.srt", arabic_srt(8))
    lines = arabic_srt(8).splitlines()
    lines[-1] = lines[2]
    duplicated = write_subtitle(tmp_path, "duplicated.srt", "\n".join(lines))
    clean_report = evaluate_subtitle(
        clean,
        requested_language="ar",
        media_duration=480,
        compatibility=0,
        compatibility_known=True,
    )
    fit_heavy_report = evaluate_subtitle(
        duplicated,
        requested_language="ar",
        media_duration=480,
        compatibility=100,
        compatibility_known=True,
    )

    assert fit_heavy_report.factor("release_fit").points == 10
    assert clean_report.content_score > fit_heavy_report.content_score
    assert choose_candidate([clean_report, fit_heavy_report]).chosen_index == 0


def test_fit_cannot_make_a_low_quality_candidate_eligible(tmp_path):
    subtitle = write_subtitle(
        tmp_path,
        "sparse.srt",
        "1\n00:00:01,000 --> 00:00:03,000\nمرحبا بالعالم\n\n"
        "broken cue block\n",
    )

    report = evaluate_subtitle(
        subtitle,
        requested_language="ar",
        compatibility=100,
        compatibility_known=True,
    )

    assert report.score >= 60
    assert report.content_score < 60
    assert report.accepted is False


@pytest.mark.parametrize(
    "content",
    [
        "1\n00:00:01,000 --> 00:00:02,000\nHello ÿÿ\n",
        "1\n00:00:01,000 --> 00:00:02,000\nHello \ufffd world\n",
    ],
)
def test_encoding_corruption_is_scored_as_content_damage(tmp_path, content):
    subtitle = write_subtitle(tmp_path, "damaged.srt", content)

    report = evaluate_subtitle(
        subtitle,
        requested_language="en",
        media_duration=10,
    )

    assert report.factor("language_integrity").points < 15
    assert report.factor("language_integrity").details
