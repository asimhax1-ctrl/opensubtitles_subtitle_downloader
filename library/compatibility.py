"""How well a subtitle release matches the media file it was found for.

The percentage is *verified compatibility evidence*, not a probability. Every
facet of an ideal complete match is worth a fixed number of points; a facet the
two names agree on earns them, a facet neither name states earns nothing, and a
facet they contradict loses its points and, for the facets that decide what the
subtitle is even for, caps the result outright.

Nothing is renormalised over the facets that happen to be present. A release
name that verifies a title and a year and nothing else scores the thirty-odd
points that title and year are worth, never a hundred -- otherwise a sparse
match would read as a perfect one. Only facets that cannot exist for the media
are left out of the denominator: a film has no episode to match.

That same reading decides the one place silence is not merely silence. A name
with no season and no episode is a film, so a subtitle that declares one is for
something else and is conflicted rather than left unverified.

Deliberately not inputs: download count, hearing-impaired and AI-translated
flags (signals about availability, not about fit) and provider reliability (a
tiebreak, which must never inflate a compatibility percentage).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from thefuzz import fuzz

from library.subtitle_utils import (
    APOSTROPHE_RE,
    KEEP_MATCH_CHARS_RE,
    RELEASE_SOURCE_TOKENS,
    RESOLUTION_RE,
    SOURCE_PATTERNS,
    YEAR_RE,
    SubtitleUtils,
    collapse_release_technical_tokens,
    fold_arabic,
    release_group_of,
)

# What each facet of an ideal complete match is worth. The sum is the
# denominator: a movie that verifies everything a name can state reaches the
# filename ceiling below.
TITLE_POINTS = 45
YEAR_POINTS = 10
SEASON_POINTS = 6
EPISODE_POINTS = 20
EDITION_POINTS = 7
SOURCE_POINTS = 5
RESOLUTION_POINTS = 3
GROUP_POINTS = 2
CODEC_POINTS = 2
TOTAL_POINTS = (
    TITLE_POINTS
    + YEAR_POINTS
    + SEASON_POINTS
    + EPISODE_POINTS
    + EDITION_POINTS
    + SOURCE_POINTS
    + RESOLUTION_POINTS
    + GROUP_POINTS
    + CODEC_POINTS
)
# Season and episode are the two facets a film cannot have, so they are the two
# the film denominator leaves out.
IDENTITY_POINTS = SEASON_POINTS + EPISODE_POINTS

# A title that only partly overlaps is still evidence, just weaker evidence.
TITLE_PARTIAL_RATIO = 0.75
# Above this token-set similarity, two spellings are the same title rather than
# two different ones -- transliterations and typos, mostly.
TITLE_FUZZY_FLOOR = 90

# A conflict in these facets contradicts what the subtitle is *for*, so it caps
# the percentage outright: no amount of matching elsewhere rescues a wrong film,
# a wrong episode or a different cut. The bands are the user-facing ones, so a
# cap is also the guarantee that the badge says MISMATCH.
TITLE_CONFLICT_CAP = 20
SEASON_CONFLICT_CAP = 25
EPISODE_CONFLICT_CAP = 25
EDITION_CONFLICT_CAP = 34
# A film's name has no season and no episode, so a subtitle declaring one is for
# a different thing entirely -- the same class of mistake as a wrong episode, and
# capped the same way. It is a conflict rather than a silence because the media's
# name already answered the question: it said film.
FILM_EPISODE_CONFLICT_CAP = 25
# How the media side of that conflict reads in the Conflict block. There is no
# value to quote, because the media's identity *is* the absence of a season.
FILM_IDENTITY_LABEL = "film"

# A conflict in a technical facet costs the facet's own points and this much
# again, so agreeing is always worth strictly more than staying silent.
YEAR_CONFLICT_PENALTY = 12
SOURCE_CONFLICT_PENALTY = 4
RESOLUTION_CONFLICT_PENALTY = 3
GROUP_CONFLICT_PENALTY = 2
CODEC_CONFLICT_PENALTY = 2

# Without a hash, the release name is all there is, so nothing may reach the
# band reserved for a verified file match.
FILENAME_ONLY_CEILING = 90

MISMATCH_BADGE = "MISMATCH"
# Floors, highest first. compatibility_badge() reads them in order.
BADGE_BANDS = (
    (90, "BEST"),
    (75, "GREAT"),
    (55, "GOOD"),
    (35, "MAYBE"),
)

# Facet vocabulary on top of the project's own SOURCE_PATTERNS. DVDRip is in the
# approved evidence list but not in the table, and a bare "web" tag is the most
# common source spelling of all. Appended rather than inserted into
# subtitle_utils.SOURCE_PATTERNS, whose order decides explain_subtitle_match's
# locked scores.
SOURCE_LABELS = SOURCE_PATTERNS + (("dvdrip", "DVDRip"),)
SOURCE_TOKENS = RELEASE_SOURCE_TOKENS | {"dvdrip"}

# Ordered most specific first, and the first rule that matches wins on both sides
# of a comparison, so a name stating two of them still names one edition.
EDITION_RULES = (
    (("directors", "cut"), "Director's Cut"),
    (("theatrical", "cut"), "Theatrical"),
    (("extended", "cut"), "Extended"),
    (("extended",), "Extended"),
    (("unrated",), "Unrated"),
    (("remastered",), "Remastered"),
    (("criterion",), "Criterion"),
    (("imax",), "IMAX"),
    (("dc",), "Director's Cut"),
    (("theatrical",), "Theatrical"),
)

CODEC_LABELS = (
    (("x265", "h265", "hevc"), "H.265"),
    (("x264", "h264", "avc"), "H.264"),
    (("xvid",), "XviD"),
    (("divx",), "DivX"),
    (("av1",), "AV1"),
)
CODEC_TOKENS = frozenset(
    token for tokens, _ in CODEC_LABELS for token in tokens
)

RESOLUTION_ALIASES = {"4k": "2160p"}
RESOLUTION_TOKENS = frozenset(
    ("480p", "720p", "1080p", "2160p", "4k", "8k")
)

# Words a release name carries that are not part of its title.
NOISE_WORDS = frozenset(
    {
        "8bit",
        "10bit",
        "complete",
        "dual",
        "dv",
        "episode",
        "ep",
        "hd",
        "hdr",
        "hdr10",
        "hdr10plus",
        "hearing",
        "hi",
        "impaired",
        "internal",
        "limited",
        "multi",
        "proper",
        "remux",
        "repack",
        "sdr",
        "season",
        "uhd",
        # Audio lines and variants
        "dd",
        "ddp",
        "aac",
        "ac3",
        "eac3",
        "dts",
        "dtshdma",
        "truehd",
        "atmos",
    }
)
SEASON_EPISODE_RE = re.compile(
    r"^(?:s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3})$",
    re.IGNORECASE,
)
# A season pack states a season and no episode ("Show.S01.1080p"), which is a
# complete name for a set of episodes and not a film. Matched as a whole word so
# a title that merely contains the letters "s" and a digit is left alone.
SEASON_TOKEN_RE = re.compile(r"^s(\d{1,2})$", re.IGNORECASE)
MEDIA_EXTENSION_RE = re.compile(
    r"\.(?:mkv|mp4|avi|mov|m4v|ts|webm|srt|ass|ssa|vtt|sub|zip)$",
    re.IGNORECASE,
)

# Every word that every facet detector can consume, so the title keeps only what
# is left. Built from the tables above rather than listed again, because a
# hand-written copy is what let release-group and edition fragments into the
# title in the first place.
TECHNICAL_WORDS = frozenset(
    SOURCE_TOKENS
    | CODEC_TOKENS
    | RESOLUTION_TOKENS
    | NOISE_WORDS
    | frozenset(
        part
        for token in SOURCE_TOKENS
        for part in KEEP_MATCH_CHARS_RE.sub(" ", token).split()
    )
)

# Facets whose *absence* is worth stating. Everything else that is unknown simply
# earns nothing: eight "unknown" lines to explain one weak percentage would bury
# the two that change what the number means.
REPORTED_ABSENCES = frozenset({"edition", "title"})

# Names that describe a file rather than a title, so the folder it sits in is the
# better evidence.
GENERIC_MEDIA_NAMES = frozenset(
    {
        "film",
        "index",
        "main",
        "movie",
        "movies",
        "output",
        "sample",
        "subs",
        "subtitles",
        "title",
        "trailer",
        "video",
    }
)


@dataclass(frozen=True)
class Signal:
    """One line of evidence: what was checked and whether it verified."""

    label: str
    matched: bool
    points: float = 0.0

    @property
    def line(self) -> str:
        return f"{'+' if self.matched else '-'} {self.label}"


@dataclass(frozen=True)
class Compatibility:
    """A percentage, the band it falls in, and the evidence behind both."""

    percent: int
    badge: str
    signals: tuple[Signal, ...] = ()
    conflicts: tuple[str, ...] = ()

    def evidence_lines(self) -> tuple[str, ...]:
        """The block the panes render: the conflicts, or the evidence.

        A conflict is stated instead of the evidence because it is the answer to
        "why is this so low", and the evidence that did verify is already counted
        by the percentage above it.
        """
        if self.conflicts:
            return ("Conflict:",) + self.conflicts
        if not self.signals:
            return ()
        return ("Evidence:",) + tuple(signal.line for signal in self.signals)


@dataclass(frozen=True)
class ReleaseFacets:
    """What a release name states about itself. ``None`` means it states nothing."""

    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    edition: str | None = None
    source: str | None = None
    resolution: str | None = None
    codec: str | None = None
    group: str | None = None

    @property
    def evidence_count(self) -> int:
        """How many facets this name actually states."""
        return sum(
            1
            for value in (
                self.title,
                self.year,
                self.season,
                self.episode,
                self.edition,
                self.source,
                self.resolution,
                self.codec,
                self.group,
            )
            if value
        )


@dataclass(frozen=True)
class _FacetRule:
    """One comparable facet, and what its evidence is worth."""

    name: str
    points: int
    label: str
    conflict_label: str = "{}"
    conflict_penalty: int = 0
    conflict_cap: int | None = None


@dataclass(frozen=True)
class _Outcome:
    points: float
    signal: Signal | None = None
    conflict: tuple[str, str] | None = None
    cap: int | None = None


# ``label`` reads a matched facet in the evidence block ("+ year 1984");
# ``conflict_label`` names both sides in a Conflict block, where the media and
# subtitle pair already says which facet is meant and the word would only repeat
# it ("media: Theatrical" rather than "media: edition Theatrical").
FACET_RULES = (
    _FacetRule(
        "year",
        YEAR_POINTS,
        "year {}",
        conflict_penalty=YEAR_CONFLICT_PENALTY,
    ),
    _FacetRule(
        "season",
        SEASON_POINTS,
        "season S{:02d}",
        conflict_label="S{:02d}",
        conflict_cap=SEASON_CONFLICT_CAP,
    ),
    _FacetRule(
        "episode",
        EPISODE_POINTS,
        "episode E{:02d}",
        conflict_label="E{:02d}",
        conflict_cap=EPISODE_CONFLICT_CAP,
    ),
    _FacetRule(
        "edition",
        EDITION_POINTS,
        "edition {}",
        conflict_cap=EDITION_CONFLICT_CAP,
    ),
    _FacetRule(
        "source",
        SOURCE_POINTS,
        "{}",
        conflict_penalty=SOURCE_CONFLICT_PENALTY,
    ),
    _FacetRule(
        "resolution",
        RESOLUTION_POINTS,
        "{}",
        conflict_penalty=RESOLUTION_CONFLICT_PENALTY,
    ),
    _FacetRule(
        "group",
        GROUP_POINTS,
        "group {}",
        conflict_penalty=GROUP_CONFLICT_PENALTY,
    ),
    _FacetRule(
        "codec",
        CODEC_POINTS,
        "{}",
        conflict_penalty=CODEC_CONFLICT_PENALTY,
    ),
)


def compatibility_badge(percent: int) -> str:
    """The band a percentage falls in."""
    for floor, badge in BADGE_BANDS:
        if percent >= floor:
            return badge
    return MISMATCH_BADGE


def parse_release_facets(name: object) -> ReleaseFacets:
    """Read every facet a release name states. States nothing it cannot read."""
    text = MEDIA_EXTENSION_RE.sub("", str(name or "").strip())
    text = collapse_release_technical_tokens(text)
    words = _words(text)
    year_text = _year_of(text)
    year = int(year_text) if year_text else None
    edition = _edition_of(words, text=text)
    resolution = _resolution_of(text)
    codec = _codec_of(words)
    source, source_token = _source_of(text)
    group = _group_of(text)
    season, episode = _episode_of(text)

    return ReleaseFacets(
        title=" ".join(
            _title_words(
                words,
                year_text=year_text,
                season=season,
                episode=episode,
                edition=edition,
                group=group,
                source_token=source_token,
            )
        ),
        year=year,
        season=season,
        episode=episode,
        edition=edition,
        source=source,
        resolution=resolution,
        codec=codec,
        group=group,
    )


def compatibility(
    release: object,
    media_name: object,
    hash_match: bool = False,
) -> Compatibility:
    """How well ``release`` matches the media named by ``media_name``.

    ``hash_match`` is the one piece of evidence that verifies the file itself
    rather than its name, so it short-circuits everything else.
    """
    if hash_match:
        return Compatibility(
            percent=100,
            badge=compatibility_badge(100),
            signals=(Signal("exact file hash", True, float(TOTAL_POINTS)),),
        )
    if not str(release or "").strip() or not str(media_name or "").strip():
        return Compatibility(percent=0, badge=compatibility_badge(0))

    subtitle = parse_release_facets(release)
    media = parse_release_facets(media_name)

    points = 0.0
    signals: list[Signal] = []
    conflicts: list[str] = []
    caps: list[int] = []

    for outcome in (
        _compare_title(media, subtitle),
        _compare_identity_kind(media, subtitle),
    ) + tuple(
        _compare_facet(rule, getattr(media, rule.name), getattr(subtitle, rule.name))
        for rule in FACET_RULES
    ):
        points += outcome.points
        if outcome.signal is not None:
            signals.append(outcome.signal)
        if outcome.conflict is not None:
            conflicts.extend(outcome.conflict)
        if outcome.cap is not None:
            caps.append(outcome.cap)

    signals.append(Signal("no hash match", False))

    percent = round(points / _ideal_points(media) * 100)
    percent = max(0, min(100, min(percent, FILENAME_ONLY_CEILING, *caps)))
    return Compatibility(
        percent=percent,
        badge=compatibility_badge(percent),
        signals=tuple(signals),
        conflicts=tuple(conflicts),
    )


def media_evidence_name(media_path: object) -> str:
    """The best text identity a media file offers, or "" when it offers none.

    The file name is the evidence of first resort, because a release name says
    everything a release name can say. A file called ``movie.mkv`` says nothing,
    so its folder is asked instead. Returns "" rather than a generic word, so the
    caller can fall back to whatever else it has.
    """
    if not media_path:
        return ""
    path = Path(str(media_path))
    stem_facets = parse_release_facets(path.stem)
    folder_name = path.parent.name
    folder_facets = parse_release_facets(folder_name)

    if stem_facets.title and stem_facets.title not in GENERIC_MEDIA_NAMES:
        if folder_facets.evidence_count > stem_facets.evidence_count:
            return folder_name
        return path.stem
    if folder_facets.title and folder_facets.title not in GENERIC_MEDIA_NAMES:
        return folder_name
    return ""


def _ideal_points(media: ReleaseFacets) -> int:
    """The points an ideal complete match could earn for this media.

    A film has no season or episode to match, so its ideal does not include one.
    This is the only thing left out of the denominator: a facet the media *could*
    state but the names do not is still counted, and earns nothing.
    """
    if media.season is None and media.episode is None:
        return TOTAL_POINTS - IDENTITY_POINTS
    return TOTAL_POINTS


def _compare_title(media: ReleaseFacets, subtitle: ReleaseFacets) -> _Outcome:
    media_tokens = SubtitleUtils._informative_title_tokens(media.title)
    subtitle_tokens = SubtitleUtils._informative_title_tokens(subtitle.title)

    if not media_tokens and not subtitle_tokens:
        return _Outcome(0.0, Signal("title unknown", False))
    if not subtitle_tokens:
        return _Outcome(0.0, Signal("title not stated", False))
    if not media_tokens:
        return _Outcome(0.0, Signal("title unknown", False))

    if media_tokens == subtitle_tokens:
        return _Outcome(
            float(TITLE_POINTS),
            Signal("exact title", True, float(TITLE_POINTS)),
        )
    shared = media_tokens & subtitle_tokens
    if shared or fuzz.token_set_ratio(media.title, subtitle.title) >= TITLE_FUZZY_FLOOR:
        partial = round(TITLE_POINTS * TITLE_PARTIAL_RATIO)
        return _Outcome(
            float(partial),
            Signal("partial title", True, float(partial)),
        )
    return _Outcome(
        0.0,
        Signal("title conflict", False),
        (f"media: {media.title}", f"subtitle: {subtitle.title}"),
        TITLE_CONFLICT_CAP,
    )


def _compare_identity_kind(media: ReleaseFacets, subtitle: ReleaseFacets) -> _Outcome:
    """Whether the subtitle is for the same *kind* of thing as the media.

    A film's name states no season and no episode, and that absence is what makes
    it a film -- the same reading ``_ideal_points`` uses for the denominator. So a
    subtitle that declares one is for a different thing, and is reported as a
    conflict: the media did not fail to say, it said film.

    The rule is deliberately one-sided. A media name that states a season but no
    episode is a season pack, which *contains* every episode in it, so an episode
    subtitle is compatible and stays unverified rather than conflicting.
    """
    if media.season is not None or media.episode is not None:
        return _Outcome(0.0)
    claimed = _identity_label(subtitle)
    if claimed is None:
        return _Outcome(0.0)
    return _Outcome(
        0.0,
        Signal("episode identity conflict", False),
        (f"media: {FILM_IDENTITY_LABEL}", f"subtitle: {claimed}"),
        FILM_EPISODE_CONFLICT_CAP,
    )


def _identity_label(facets: ReleaseFacets) -> str | None:
    """The episode identity a name declares, as ``S01E03``, ``S01`` or ``E03``.

    One label for both facets, because the conflict is about the identity as a
    whole; pairing the media with "S01" and then again with "E03" would report
    one mistake twice.
    """
    season = f"S{facets.season:02d}" if facets.season is not None else ""
    episode = f"E{facets.episode:02d}" if facets.episode is not None else ""
    return f"{season}{episode}" or None


def _compare_facet(
    rule: _FacetRule,
    media_value: object,
    subtitle_value: object,
) -> _Outcome:
    if media_value is None and subtitle_value is None:
        return _Outcome(0.0, _absence(rule, "unknown"))
    if subtitle_value is None:
        return _Outcome(0.0, _absence(rule, "not stated"))
    if media_value is None:
        return _Outcome(0.0, _absence(rule, "unverified"))
    if media_value == subtitle_value:
        return _Outcome(
            float(rule.points),
            Signal(rule.label.format(media_value), True, float(rule.points)),
        )
    # Release-group and ordinary technical differences are not identity
    # conflicts: they should not hide the title/year/source evidence that made
    # the result a match in the first place.
    if rule.name == "group":
        return _Outcome(
            -float(rule.conflict_penalty),
            Signal("different group", False),
        )
    return _Outcome(
        -float(rule.conflict_penalty),
        Signal(f"{rule.name} conflict", False),
        (
            f"media: {rule.conflict_label.format(media_value)}",
            f"subtitle: {rule.conflict_label.format(subtitle_value)}",
        ),
        rule.conflict_cap,
    )


def _absence(rule: _FacetRule, wording: str) -> Signal | None:
    """The line for a facet that verified nothing, when the absence matters."""
    if rule.name not in REPORTED_ABSENCES:
        return None
    return Signal(f"{rule.name} {wording}", False)


def _words(text: str) -> list[str]:
    """The name's words, folded the way the rest of the project folds them."""
    folded = fold_arabic(APOSTROPHE_RE.sub("", str(text or ""))).lower()
    return KEEP_MATCH_CHARS_RE.sub(" ", folded).split()


def _title_words(
    words: list[str],
    *,
    year_text: str | None,
    season: int | None,
    episode: int | None,
    edition: str | None,
    group: str | None,
    source_token: str | None = None,
) -> list[str]:
    """What is left of a name once every facet it states has been taken out.

    Codec, resolution and noise tokens are always removed. Source tokens are
    only removed when they were actually matched by ``_source_of``; this keeps
    real title words such as ``web`` in *Charlotte Web* while still stripping
    the source tag from *WEB-DL*. Only the specific year text that was chosen
    is removed, so year-titled films keep their title year.
    """
    consumed = set(CODEC_TOKENS | RESOLUTION_TOKENS | NOISE_WORDS)
    if source_token is not None:
        consumed.update(
            part
            for part in KEEP_MATCH_CHARS_RE.sub(" ", source_token).lower().split()
        )
    if year_text is not None:
        consumed.add(year_text)
    if season is not None:
        consumed.add(str(season))
    if episode is not None:
        consumed.add(str(episode))
    if edition is not None:
        consumed.update(_edition_words(edition))
    if group is not None:
        consumed.update(_words(group))

    return [
        word
        for word in words
        if word not in consumed
        and not SEASON_EPISODE_RE.match(word)
        and not SEASON_TOKEN_RE.match(word)
    ]


def _edition_words(edition: str) -> frozenset[str]:
    """The words that spelled the edition the same rules would read again."""
    for tokens, label in EDITION_RULES:
        if label == edition:
            return frozenset(tokens)
    return frozenset()


def _year_of(text: str) -> str | None:
    """Choose the year most consistent with a release name.

    Year-titled films ("2012.2009.1080p") break if the first year wins, because
    the title year is removed and the actual release year is left in the title.
    Prefer a year adjacent to a resolution/source/codec token; otherwise fall
    back to the last plausible year so the leading title year survives.
    """
    text_str = str(text or "")
    matches = list(YEAR_RE.finditer(text_str))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].group(0)

    words = _words(text_str)
    technical = frozenset(
        {
            "480p",
            "720p",
            "1080p",
            "2160p",
            "4k",
            "8k",
            "bluray",
            "brrip",
            "webdl",
            "web-dl",
            "webrip",
            "hdtv",
            "hdrip",
            "remux",
            "dvdrip",
            "x264",
            "x265",
            "h264",
            "h265",
            "hevc",
            "avc",
        }
    )
    indexed_words = {
        match.group(0): index
        for index, match in enumerate(
            re.finditer(r"[0-9A-Za-z]+", text_str.lower())
        )
    }

    def score(match: re.Match) -> tuple[bool, int]:
        word_index = indexed_words.get(match.group(0), len(words))
        near_technical = any(
            word.lower() in technical
            for word in words[max(0, word_index - 2) : word_index + 3]
        )
        return (near_technical, match.start())

    # Prefer technical-context years; among those, prefer the last one (release
    # names usually put the real year closest to the quality tokens).
    scored = sorted(matches, key=score)
    return scored[-1].group(0)


def _episode_of(text: str) -> tuple[int | None, int | None]:
    """Season and episode as the project's own reader finds them.

    Delegated so that the two features cannot disagree about which part of a name
    is the episode number. The project's reader reports no episode evidence at all
    for a name that states a season without one, though, and a season pack is a
    complete name rather than an absent one -- so it is read here as the season it
    states. Left unread, a season pack would be indistinguishable from a film.
    """
    season, episode, confidence = SubtitleUtils._episode_evidence(str(text or ""))
    if confidence != "none":
        return season, episode
    for word in _words(text):
        match = SEASON_TOKEN_RE.match(word)
        if match:
            return int(match.group(1)), None
    return None, None


def _edition_of(words: list[str], *, text: str = "") -> str | None:
    pairs = set(zip(words, words[1:], strict=False))
    lowered = str(text or "").lower()
    for tokens, label in EDITION_RULES:
        if len(tokens) == 2:
            if tokens in pairs:
                return label
            continue
        token = tokens[0]
        if token not in words:
            continue
        # Single-token edition abbreviations are prone to false positives on
        # titles ("DC League of Super-Pets", "IMAX ..."). Require them to sit
        # in a release context: after a year, resolution, source, or codec token.
        if token in ("dc", "imax"):
            # Use the last occurrence so edition abbreviations placed at the end
            # of a release name are detected while leading title words are not.
            last_index = lowered.rfind(token)
            if last_index > 0 and re.search(
                r"(?:19|20)\d{2}|\d{3,4}p|4k|bluray|web-?dl|webrip|"
                r"x264|x265|h264|h265|hevc|avc",
                lowered[:last_index],
            ):
                return label
            continue
        return label
    return None


def _codec_of(words: list[str]) -> str | None:
    for tokens, label in CODEC_LABELS:
        if any(token in words for token in tokens):
            return label
    return None


def _resolution_of(text: str) -> str | None:
    match = RESOLUTION_RE.search(str(text or "").lower())
    if not match:
        return None
    value = match.group(1).lower()
    return RESOLUTION_ALIASES.get(value, value)


def _source_of(text: str) -> tuple[str | None, str | None]:
    lowered = str(text or "").lower()
    for token, label in SOURCE_LABELS:
        if "-" in token or len(token) > 4:
            # Compound/hyphenated tokens and longer service names are unlikely to
            # appear inside real title words, so a substring search is safe.
            if token in lowered:
                return label, token
            continue
        # Short bare tokens such as "web" must match as whole words; otherwise
        # titles like "Charlotte Web" are mislabelled as WEB releases.
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
            return label, token
    return None, None


def _group_of(text: str) -> str | None:
    """The release group, which is the token after the last hyphen.

    A hyphenated source ("WEB-DL") also ends in a hyphenated token, so the text
    before the hyphen is checked against the known sources first: otherwise the
    source would be read as the group and left unknown.
    """
    # Shared with the query-side title hypotheses in subtitle_utils so the
    # ranker and the provider queries agree on where the title ends.
    return release_group_of(text)
