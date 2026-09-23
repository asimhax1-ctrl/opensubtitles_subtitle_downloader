"""Content-first subtitle evaluation for automatic candidate selection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from charset_normalizer import from_bytes

FACTOR_WEIGHTS = {
    "structural_validity": 20,
    "completeness": 20,
    "readability": 20,
    "timing_sanity": 15,
    "language_integrity": 15,
    "release_fit": 10,
}

_ARABIC_RE = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff"
    r"\ufb50-\ufdff\ufe70-\ufeff]"
)
_LATIN_RE = re.compile(r"[A-Za-z]")
_SRT_TIME = re.compile(
    r"^(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})"
    r"\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})(?:\s+.*)?$"
)
_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.](\d{1,2})$")
_MICRODVD = re.compile(r"^\{(\d+)\}\{(\d+)\}(.*)$")
_ASS_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]*>")
_MOJIBAKE = re.compile(r"(?:Ã.|Â.|Ð.|Ø.|Ù.|â€.)|ÿ{2,}")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class FactorScore:
    name: str
    points: float
    maximum: int
    known: bool
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityReport:
    score: int
    accepted: bool
    hard_rejection: str | None
    factors: tuple[FactorScore, ...]
    confidence: float

    def factor(self, name: str) -> FactorScore:
        return next(factor for factor in self.factors if factor.name == name)

    @property
    def content_score(self) -> float:
        return sum(
            factor.points for factor in self.factors if factor.name != "release_fit"
        )


@dataclass(frozen=True)
class SelectionDecision:
    chosen_index: int | None
    manual_required: bool
    reason: str
    rejected_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Parsed:
    fmt: str
    cues: tuple[Cue, ...]
    total_blocks: int
    malformed: int
    frame_based: bool = False
    ordering_issues: int = 0


def _decode(path: Path) -> tuple[str, bool, tuple[str, ...]]:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            match = from_bytes(data).best()
            text = (
                str(match)
                if match is not None
                else data.decode("utf-8", errors="replace")
            )
    details = []
    if "\ufffd" in text:
        details.append("replacement characters indicate decoding damage")
    mojibake = len(_MOJIBAKE.findall(text))
    if mojibake:
        details.append(f"{mojibake} likely mojibake sequence(s)")
    has_control = any(
        unicodedata.category(char) == "Cc" and char not in "\n\r\t"
        for char in text
    )
    if has_control:
        details.append("unexpected control characters present")
    return text, bool(details), tuple(details)


def _time(match: re.Match, offset: int = 1) -> float:
    hours = int(match.group(offset) or 0)
    minutes = int(match.group(offset + 1))
    seconds = int(match.group(offset + 2))
    fraction = int(match.group(offset + 3).ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + fraction / 1000


def _srt_timestamps_valid(match: re.Match) -> bool:
    return all(
        int(match.group(index)) < 60 for index in (2, 3, 6, 7)
    )


def _visible_text(text: str, fmt: str) -> str:
    if fmt in {"ass", "ssa"}:
        text = _ASS_TAG.sub("", text)
        text = re.sub(r"\\[Nn]", "\n", text).replace(r"\h", " ")
    if fmt == "sub":
        text = text.replace("|", "\n")
    text = _HTML_TAG.sub("", text)
    return text.strip()


def _parse_srt_vtt(text: str, fmt: str) -> _Parsed:
    source = text.lstrip("\ufeff")
    if fmt == "vtt":
        if not source.startswith("WEBVTT"):
            return _Parsed(fmt, (), 1, 1)
        source = source.split("\n", 1)[1] if "\n" in source else ""
    blocks = [block for block in re.split(r"\r?\n\s*\r?\n", source) if block.strip()]
    cues: list[Cue] = []
    malformed = 0
    considered = 0
    for block in blocks:
        rows = [row.strip() for row in block.splitlines() if row.strip()]
        is_vtt_metadata = fmt == "vtt" and rows and rows[0].startswith(
            ("NOTE", "STYLE", "REGION")
        )
        if not rows or is_vtt_metadata:
            continue
        considered += 1
        time_row = next((row for row in rows if "-->" in row), None)
        match = _SRT_TIME.fullmatch(time_row or "")
        if match is None or not _srt_timestamps_valid(match):
            malformed += 1
            continue
        start, end = _time(match, 1), _time(match, 5)
        cue_text = _visible_text("\n".join(rows[rows.index(time_row) + 1 :]), fmt)
        if end <= start or not cue_text:
            malformed += 1
            continue
        cues.append(Cue(start, end, cue_text))
    ordering_issues = sum(
        cues[index].start < cues[index - 1].start
        for index in range(1, len(cues))
    )
    return _Parsed(
        fmt,
        tuple(cues),
        max(considered, len(cues)),
        malformed,
        ordering_issues=ordering_issues,
    )


def _parse_ass(text: str, fmt: str) -> _Parsed:
    section = ""
    fields: list[str] | None = None
    cues: list[Cue] = []
    total = malformed = 0
    for line in text.splitlines():
        row = line.strip()
        if row.startswith("[") and row.endswith("]"):
            section = row.lower()
        elif section == "[events]" and row.lower().startswith("format:"):
            fields = [
                field.strip().lower()
                for field in row.split(":", 1)[1].split(",")
            ]
        elif section == "[events]" and row.lower().startswith("dialogue:"):
            total += 1
            if fields is None or not {"start", "end", "text"}.issubset(fields):
                malformed += 1
                continue
            values = row.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
            if len(values) != len(fields):
                malformed += 1
                continue
            start_match = _ASS_TIME.fullmatch(values[fields.index("start")].strip())
            end_match = _ASS_TIME.fullmatch(values[fields.index("end")].strip())
            visible = _visible_text(values[fields.index("text")], fmt)
            if (
                start_match is None
                or end_match is None
                or int(start_match.group(2)) >= 60
                or int(start_match.group(3)) >= 60
                or int(end_match.group(2)) >= 60
                or int(end_match.group(3)) >= 60
                or not visible
            ):
                malformed += 1
                continue
            start = _time(start_match)
            end = _time(end_match)
            if end <= start:
                malformed += 1
                continue
            cues.append(Cue(start, end, visible))
    ordering_issues = sum(
        cues[index].start < cues[index - 1].start
        for index in range(1, len(cues))
    )
    return _Parsed(
        fmt,
        tuple(cues),
        max(total, len(cues)),
        malformed,
        ordering_issues=ordering_issues,
    )


def _parse_microdvd(text: str) -> _Parsed:
    cues: list[Cue] = []
    malformed = 0
    total = 0
    ordering_issues = 0
    previous_start = -1
    for row in text.splitlines():
        if not row.strip():
            continue
        total += 1
        match = _MICRODVD.fullmatch(row.strip())
        if match is None:
            malformed += 1
            continue
        start_frame, end_frame = int(match.group(1)), int(match.group(2))
        visible = _visible_text(match.group(3), "sub")
        if end_frame <= start_frame or not visible:
            malformed += 1
            continue
        if start_frame < previous_start:
            ordering_issues += 1
        previous_start = start_frame
        cues.append(Cue(float(start_frame), float(end_frame), visible))
    return _Parsed(
        "sub",
        tuple(cues),
        total,
        malformed,
        frame_based=True,
        ordering_issues=ordering_issues,
    )


def _parse(text: str, suffix: str) -> _Parsed:
    fmt = suffix.lower().lstrip(".")
    if fmt in {"ass", "ssa"}:
        return _parse_ass(text, fmt)
    if fmt == "vtt" or fmt == "srt":
        return _parse_srt_vtt(text, fmt)
    if fmt == "sub":
        return _parse_microdvd(text)
    return _parse_srt_vtt(text, "srt")


def _factor(
    name: str,
    points: float,
    known: bool,
    *details: str,
) -> FactorScore:
    maximum = FACTOR_WEIGHTS[name]
    return FactorScore(
        name,
        max(0.0, min(float(maximum), points)),
        maximum,
        known,
        tuple(details),
    )


def _readability(cues: tuple[Cue, ...], fmt: str) -> float:
    if not cues:
        return 0
    values = []
    for cue in cues:
        lines = cue.text.splitlines() or [cue.text]
        longest = max((len(line) for line in lines), default=0)
        score = 1.0
        if longest > 42:
            score -= min(0.55, (longest - 42) / 100)
        if len(lines) > 2:
            score -= min(0.35, (len(lines) - 2) * 0.12)
        words = sum(len(line.split()) for line in lines)
        if words == 1 and len(lines[0]) < 3:
            score -= 0.2
        if fmt in {"ass", "ssa"} and not cue.text.strip():
            score = 0
        values.append(max(0.0, score))
    return 20 * sum(values) / len(values)


def _timing_score(cues: tuple[Cue, ...], frame_based: bool) -> float:
    if not cues:
        return 0
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    duplicate_count = 0
    normalized = set()
    for cue in cues:
        key = " ".join(cue.text.casefold().split())
        if key in normalized:
            duplicate_count += 1
        normalized.add(key)
    overlap_count = 0
    for index in range(1, len(ordered)):
        previous, current = ordered[index - 1], ordered[index]
        overlap = previous.end - current.start
        shorter_cue = min(
            previous.end - previous.start,
            current.end - current.start,
        )
        if overlap > 0 and overlap / max(0.001, shorter_cue) > 0.5:
            overlap_count += 1
    duration_count = (
        0 if frame_based else sum(cue.end - cue.start > 15.0 for cue in cues)
    )
    n = len(cues)
    return max(
        0,
        (11 if frame_based else 15)
        - 8 * duplicate_count / n
        - 4 * overlap_count / max(1, n - 1)
        - 3 * duration_count / n,
    )


def _outside_media_penalty(cues: tuple[Cue, ...], media_duration: float) -> float:
    if not cues or media_duration <= 0:
        return 0
    late = sum(cue.start > media_duration + 300 for cue in cues)
    overrun = sum(cue.end > media_duration + 300 for cue in cues)
    return min(2.0, (late + overrun) / len(cues))


def evaluate_subtitle(
    subtitle_path: str | Path,
    *,
    requested_language: str,
    media_duration: float | None = None,
    compatibility: int | None = None,
    compatibility_known: bool = False,
) -> QualityReport:
    """Score the actual subtitle file without modifying its bytes or format."""
    path = Path(subtitle_path)
    text, corrupted, encoding_details = _decode(path)
    parsed = _parse(text, path.suffix)
    cues = parsed.cues
    denominator = max(1, parsed.total_blocks)
    structure_points = FACTOR_WEIGHTS["structural_validity"] * len(cues) / denominator
    structural = _factor(
        "structural_validity",
        structure_points * max(0.0, 1 - parsed.ordering_issues / denominator),
        parsed.total_blocks > 0,
        f"{len(cues)} valid cue(s), {parsed.malformed} malformed, "
        f"{parsed.ordering_issues} out-of-order cue(s)",
    )

    if parsed.frame_based or media_duration is None or media_duration <= 0:
        completeness = _factor(
            "completeness", 0, False, "media-duration coverage is unavailable"
        )
    else:
        coverage = 0.0
        if cues:
            span = max(cue.end for cue in cues) - min(cue.start for cue in cues)
            coverage = min(1.0, max(0.0, span / media_duration))
        completeness = _factor(
            "completeness",
            min(20.0, coverage / 0.75 * 20),
            True,
            f"subtitle spans {coverage:.0%} of known media duration",
        )

    readability = _factor(
        "readability",
        _readability(cues, parsed.fmt),
        bool(cues),
        "readable line length and cue segmentation",
    )
    timing_points = _timing_score(cues, parsed.frame_based)
    if cues and media_duration is not None and not parsed.frame_based:
        timing_points -= _outside_media_penalty(cues, media_duration)
    elif cues and not parsed.frame_based:
        # Absolute placement cannot be checked without knowing the media length.
        timing_points = min(timing_points, 14)
    timing = _factor(
        "timing_sanity",
        timing_points,
        bool(cues) and not parsed.frame_based and media_duration is not None,
        (
            "duplicate and frame-overlap checks; duration in seconds "
            "unknown without FPS"
            if parsed.frame_based
            else "duplicate, overlap, duration, and media-boundary checks"
            if media_duration is not None
            else "duration checked; media-boundary evidence unavailable"
        ),
    )
    visible_text = " ".join(cue.text for cue in cues)
    arabic_chars = len(_ARABIC_RE.findall(visible_text))
    letters = sum(char.isalpha() for char in visible_text)
    language = requested_language.strip().lower().split("-", 1)[0]
    if language == "ar":
        language_ok = arabic_chars > 0 and arabic_chars / max(1, letters) >= 0.08
    elif language in {"en", "fr", "de", "es", "it", "pt", "nl"}:
        language_ok = bool(_LATIN_RE.search(visible_text))
    else:
        language_ok = bool(visible_text)
    language_points = 15.0 if language_ok else 0.0
    if corrupted:
        language_points *= 0.5
    language_factor = _factor(
        "language_integrity",
        language_points,
        bool(cues),
        *(encoding_details or ("Arabic/script integrity checked",)),
    )
    fit_known = compatibility_known and compatibility is not None
    fit_points = (
        FACTOR_WEIGHTS["release_fit"] * max(0, min(100, compatibility)) / 100
        if fit_known
        else 0
    )
    fit_factor = _factor(
        "release_fit",
        fit_points,
        fit_known,
        (
            "compatibility metadata is secondary"
            if fit_known
            else "no measured release/Fit evidence"
        ),
    )
    factors = (
        structural,
        completeness,
        readability,
        timing,
        language_factor,
        fit_factor,
    )
    score = round(sum(factor.points for factor in factors))
    confidence = sum(
        factor.maximum for factor in factors if factor.known
    ) / sum(FACTOR_WEIGHTS.values())
    hard_rejection = None
    if not cues:
        hard_rejection = "zero_valid_timed_cues"
    elif not language_ok and (
        language == "ar"
        or language in {"en", "fr", "de", "es", "it", "pt", "nl"}
    ):
        hard_rejection = "wrong_language"
    return QualityReport(
        score=score,
        accepted=hard_rejection is None and sum(
            factor.points for factor in factors if factor.name != "release_fit"
        ) >= 60,
        hard_rejection=hard_rejection,
        factors=factors,
        confidence=confidence,
    )


def choose_candidate(reports: list[QualityReport]) -> SelectionDecision:
    """Choose confidently on file content, using Fit only for a content tie."""
    rejected = tuple(
        index for index, report in enumerate(reports) if not report.accepted
    )
    eligible = [
        index for index, report in enumerate(reports) if report.accepted
    ]
    if not eligible:
        return SelectionDecision(
            None, True, "no candidate passed quality checks", rejected
        )

    eligible.sort(
        key=lambda index: (
            reports[index].content_score,
            reports[index].factor("release_fit").points,
            reports[index].score,
        ),
        reverse=True,
    )
    winner_index = eligible[0]
    winner = reports[winner_index]
    if len(eligible) == 1:
        if winner.content_score >= 75:
            return SelectionDecision(
                winner_index,
                False,
                "only eligible candidate meets quality threshold",
                rejected,
            )
        return SelectionDecision(
            None, True, "candidate quality is below auto threshold", rejected
        )

    runner_up = reports[eligible[1]]
    content_factors = (
        "structural_validity",
        "completeness",
        "readability",
        "timing_sanity",
        "language_integrity",
    )
    winner_advantages = set()
    runner_advantages = set()
    for name in content_factors:
        left, right = winner.factor(name), runner_up.factor(name)
        if not left.known or not right.known:
            continue
        if left.points - right.points >= 5:
            winner_advantages.add(name)
        elif right.points - left.points >= 5:
            runner_advantages.add(name)
    conflict = bool(winner_advantages and runner_advantages)
    if winner.content_score >= 85 and not conflict:
        return SelectionDecision(
            winner_index,
            False,
            "excellent content quality without a material quality conflict",
            rejected,
        )
    lead = winner.content_score - runner_up.content_score
    if winner.content_score >= 75 and lead >= 8 and not conflict:
        return SelectionDecision(
            winner_index,
            False,
            "quality threshold and lead are clear without a material conflict",
            rejected,
        )
    return SelectionDecision(
        None,
        True,
        "candidate quality is ambiguous; ask for a manual choice",
        rejected,
    )
