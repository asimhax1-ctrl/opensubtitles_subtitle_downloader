# subtitle_utils.py

import os
import pickle
import re
import struct
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table
from thefuzz import fuzz

import library.clean_subtitles as clean_subtitles
import library.sync_subtitles as sync_subtitles

# ================================ Paths =============================
CURRENT_DIR_PATH = os.path.dirname(os.path.realpath(__file__))
TOKEN_STORAGE_FILE = os.path.join(CURRENT_DIR_PATH, "token.pkl")
# ====================================================================


def report_existing_subtitle(path: str | Path, console: Console) -> bool:
    """Print a conflict message and return True when the subtitle path exists."""
    target = Path(path)
    if target.exists():
        console.print(f"[bold red]Subtitle already exists: {target}[/]")
        return True
    return False


# Release naming drops apostrophes entirely ("Widow's Bay" -> "Widows Bay"),
# so they are removed rather than treated as token separators when matching
# or building provider search queries.
APOSTROPHE_RE = re.compile(r"['\u2018\u2019\u02BB\u02BC`\u00B4]")

# Arabic orthographic folding for matching. Providers and release groups spell the
# same Arabic title many ways: with or without harakat, with any of the alef forms,
# with Arabic-Indic digits. Folding them to one canonical spelling is what lets two
# spellings of one title compare equal.
# Matching side only -- normalize_media_name() builds provider *queries*, and folding
# a query would remove matches rather than add them.
ARABIC_MARKS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
TATWEEL_RE = re.compile(r"\u0640")
ALEF_FORMS_RE = re.compile(r"[\u0622\u0623\u0625\u0671]")
ARABIC_DIGITS_RE = re.compile(r"[\u0660-\u0669\u06F0-\u06F9]")

def _abs_subsource_url(link: str) -> str:
    """Return an absolute SubSource URL, expanding a relative path if needed."""
    if not link:
        return link
    link = link.split("?")[0]
    if link.startswith("http://") or link.startswith("https://"):
        return link
    base = "https://subsource.net"
    return base + (link if link.startswith("/") else f"/{link}")


# The characters _normalize_match_text keeps. Everything outside this class becomes a
# separator, so the Arabic ranges must be listed explicitly -- otherwise the folding
# above is undone by the very next line.
KEEP_MATCH_CHARS_RE = re.compile(
    r"[^0-9A-Za-z"
    r"\u0600-\u06FF"  # Arabic
    r"\u0750-\u077F"  # Arabic Supplement
    r"\u08A0-\u08FF"  # Arabic Extended-A
    r"\uFB50-\uFDFF"  # Arabic Presentation Forms-A
    r"\uFE70-\uFEFF"  # Arabic Presentation Forms-B
    r"]+"
)


def _fold_arabic_digit(match):
    character = match.group(0)
    base = 0x06F0 if ord(character) >= 0x06F0 else 0x0660
    return chr(ord("0") + (ord(character) - base))


def fold_arabic(value):
    """Fold Arabic orthographic variants to one canonical spelling.

    Removes harakat, tatweel, and Quranic marks; unifies alef, teh marbuta, and alef
    maksura; and maps both Arabic-Indic digit blocks to ASCII. Total by construction:
    ``None``, empty, and Latin-only input pass through unchanged, and nothing here
    raises.
    """
    if not value:
        return value
    text = str(value)
    text = ARABIC_MARKS_RE.sub("", text)
    text = TATWEEL_RE.sub("", text)
    text = ALEF_FORMS_RE.sub("\u0627", text)
    text = text.replace("\u0629", "\u0647")
    text = text.replace("\u0649", "\u064A")
    return ARABIC_DIGITS_RE.sub(_fold_arabic_digit, text)


# Reasons are emitted most significant first and truncated to this many. The two
# that must never be hidden -- the episode determination and the provider -- are
# placed inside it; only source and resolution can be dropped.
MAX_MATCH_REASONS = 6

# Display labels for release-source tokens, matched against the raw release name so
# hyphenated forms survive. Ordered most specific first.
SOURCE_PATTERNS = (
    ("web-dl", "WEB-DL"),
    ("webdl", "WEB-DL"),
    ("webrip", "WEBRip"),
    ("bluray", "BluRay"),
    ("brrip", "BRRip"),
    ("hdtv", "HDTV"),
    ("hdrip", "HDRip"),
    ("remux", "REMUX"),
    ("amzn", "AMZN"),
    ("atvp", "ATVP"),
    ("dsnp", "DSNP"),
    ("netflix", "NF"),
)
RESOLUTION_RE = re.compile(r"\b(\d{3,4}p|4k)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass(frozen=True)
class MatchExplanation:
    """A release score together with the evidence that produced it."""

    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _TitleMatch:
    """A title comparison's score plus whether the fuzzy path produced it."""

    score: float
    fuzzy_matched: bool


class SubtitleUtils:
    console = Console()

    def __init__(self):
        pass

    def extract_subdl_subtitle_id(self, url):
        if not url:
            return None
        # v1: '/subtitle/3158195-3172856.zip'
        # v2: '/subtitle/3158195-3172856.zip?api_key=...'  (strip the query first)
        url = url.split("?")[0]
        parts = url.split("/")
        # 'and parts[2]' makes '/subtitle/' (empty id segment) return None, not ''
        if len(parts) >= 3 and parts[2]:
            return parts[2].replace(".zip", "")
        return None

    def extract_subsource_subtitle_id(self, subtitle):
        """Pull the numeric SubSource subtitle id from the raw API object.

        SubSource returns `subtitleId` (int) on the subtitle object directly, so we
        prefer that. The `link` field (e.g. '/subtitle/inception-2010/english/10215904')
        is used as a fallback, taking its last numeric path segment.
        """
        sub_id = subtitle.get("subtitleId")
        if sub_id is not None:
            return str(sub_id)
        link = subtitle.get("link", "") or ""
        link = link.split("?")[0].rstrip("/")
        if link:
            last = link.rsplit("/", 1)[-1]
            if last.isdigit():
                return last
        return None

    def standardize_subtitle_object(self, subtitle, backend="opensubtitles"):
        """Convert subtitle object to standard format"""
        try:
            match backend:
                case "opensubtitles":
                    return subtitle  # Already in desired format

                case "subdl":
                    url = subtitle.get("url")
                    if not url:
                        return None
                    return {
                        "id": self.extract_subdl_subtitle_id(url),
                        "attributes": {
                            "release": subtitle.get("release_name", ""),
                            "language": subtitle.get("language", "").lower(),
                            "download_count": 0,  # SubDL doesn't provide this
                            "ai_translated": False,  # SubDL doesn't provide this
                            "machine_translated": False,
                            "moviehash_match": False,
                            "url": url,
                            "hi": subtitle.get("hi", False),
                            "full_season": subtitle.get("full_season", False),
                            "author": subtitle.get("author", "Unknown"),
                            "season": subtitle.get("season"),
                            "episode": subtitle.get("episode"),
                            "unpack_files": subtitle.get("unpack_files", []),
                        },
                    }

                case "subsource":
                    sub_id = self.extract_subsource_subtitle_id(subtitle)
                    if sub_id is None:
                        return None
                    # releaseInfo is a list of release names; join them so the
                    # scorer can match against any of them.
                    release_info = subtitle.get("releaseInfo")
                    if isinstance(release_info, list):
                        release = " | ".join(r for r in release_info if r)
                    else:
                        release = release_info or ""
                    # SubSource languages are full names ("english"); keep verbatim
                    # but lower-cased for consistency with other backends.
                    language = (subtitle.get("language") or "").lower()
                    contributors = subtitle.get("contributors") or []
                    author = (
                        contributors[0].get("displayname", "Unknown")
                        if contributors and isinstance(contributors[0], dict)
                        else "Unknown"
                    )
                    prod = (subtitle.get("productionType") or "").lower()
                    return {
                        "id": sub_id,
                        "attributes": {
                            "release": release,
                            "language": language,
                            "download_count": subtitle.get("downloads", 0) or 0,
                            # Only "machine" productionType is considered AI/MT.
                            "ai_translated": prod == "machine",
                            "machine_translated": prod == "machine",
                            "moviehash_match": False,
                    # url is the human-readable link; the real download URL is
                    # built from the id by SubSource._download_url_for. Expand a
                    # relative path so the TUI "copy public URL" feature copies an
                    # absolute, shareable URL.
                    "url": _abs_subsource_url(subtitle.get("link", "")),
                            "hi": bool(subtitle.get("hearingImpaired")),
                            "full_season": False,
                            "author": author,
                            "season": None,
                            "episode": None,
                            "unpack_files": [],
                        },
                    }
        except Exception as e:
            self.console.print(f"[bold red]Error standardizing subtitle object: {e}[/]")
            return None

    def save_token(self, token):
        try:
            # Create a dictionary to store the token and the timestamp
            data = {
                "token": token,
                "timestamp": time.time(),
            }  # Store the current timestamp

            # Save the data to a pickle file
            with open(TOKEN_STORAGE_FILE, "wb") as file:
                pickle.dump(data, file)
        except Exception as e:
            self.console.print(f"[bold red]Error saving token: {e}[/]")

    def read_token(self):
        try:
            # Check if the pickle file exists
            if os.path.exists(TOKEN_STORAGE_FILE):
                with open(TOKEN_STORAGE_FILE, "rb") as file:
                    data = pickle.load(file)

                # Get the timestamp and current time
                timestamp = data["timestamp"]
                current_time = time.time()

                # Check if the token was saved less than 23 hours ago
                if current_time - timestamp < 23 * 3600:  # 23 hours in seconds
                    return data["token"]

            # If the file doesn't exist or the token is too old, return False
            return False
        except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
            self.console.print(f"[bold yellow]Warning: Error reading token: {e}[/]")
            return False
        except Exception as e:
            self.console.print(f"[bold red]Error reading token: {e}[/]")
            return False

    def clean_subtitles_strict(self, subtitle_path, ads_path=None, ads_separator=","):
        return clean_subtitles.clean_ads(
            subtitle_path,
            ads_file_path=ads_path,
            ads_separator=ads_separator,
        )

    def clean_subtitles(self, subtitle_path, ads_path=None):
        try:
            return self.clean_subtitles_strict(subtitle_path, ads_path)
        except Exception as e:
            self.console.print(f"[bold red]Error cleaning subtitles: {e}[/]")
            return False

    def sync_subtitles_strict(
        self, media_path, subtitle_path, on_output=None, cancel_event=None
    ):
        return sync_subtitles.sync_subs_audio(
            media_path,
            subtitle_path,
            on_output=on_output,
            cancel_event=cancel_event,
        )

    def sync_subtitles(self, media_path, subtitle_path):
        try:
            return self.sync_subtitles_strict(media_path, subtitle_path)
        except Exception as e:
            self.console.print(f"[bold red]Error syncing subtitles: {e}[/]")
            return False

    def sort_list_of_dicts_by_key(self, input_list, key_to_sort_by):
        try:
            # Create an empty set to store unique 'id' values
            unique_ids = set()

            # Initialize an empty list to store unique items
            unique_data = []

            # Iterate through the list of dictionaries
            for item in input_list:
                item_id = item.get("id")
                if item_id is None:
                    continue

                # Check if the 'id' is not already in the set of unique_ids
                if item_id not in unique_ids:
                    unique_ids.add(item_id)
                    unique_data.append(item)

            def _sort_key(x):
                try:
                    return x["attributes"][key_to_sort_by]
                except (KeyError, TypeError):
                    return 0

            sorted_list = sorted(unique_data, key=_sort_key, reverse=True)
            return sorted_list
        except Exception as e:
            self.console.print(f"[bold red]Unexpected error sorting list: {e}[/]")
            return []

    def hashFile(self, media_path):
        """Produce a hash for a video file: size + 64bit chksum of the first and
        last 64k (even if they overlap because the file is smaller than 128k)"""
        try:
            longlongformat = "Q"  # unsigned long long little endian
            bytesize = struct.calcsize(longlongformat)
            fmt = f"<{65536 // bytesize}{longlongformat}"

            with open(media_path, "rb") as f:
                filesize = os.fstat(f.fileno()).st_size
                filehash = filesize

                if filesize < 65536 * 2:
                    self.console.print(
                        f"[bold red]Error: File size error while generating hash for {media_path}[/]"
                    )
                    return None

                buf = f.read(65536)
                longlongs = struct.unpack(fmt, buf)
                filehash += sum(longlongs)

                f.seek(-65536, os.SEEK_END)  # size is always > 131072
                buf = f.read(65536)
                longlongs = struct.unpack(fmt, buf)
                filehash += sum(longlongs)
                filehash &= 0xFFFFFFFFFFFFFFFF

            returnedhash = "{:016x}".format(filehash)
            return returnedhash

        except OSError as e:
            self.console.print(
                f"[bold red]Error: I/O error while generating hash for {media_path}: {e}[/]"
            )
            return None
        except Exception as e:
            self.console.print(
                f"[bold red]Unexpected error generating hash for {media_path}: {e}[/]"
            )
            return None

    def extract_season_and_episode(self, media_name):
        """Extract season and episode numbers from media name.

        Delegates to the same parser used for release-name scoring so the two
        can never disagree. Daily/ aired dates are treated as episode identity
        by air date and return (None, None) so the caller falls back to the
        release name rather than inventing a season/episode pair.
        """
        if not media_name:
            return None, None

        season, episode, confidence = self._episode_evidence(
            str(media_name),
            allow_bare=True,
        )
        if confidence != "none":
            return season, episode

        # A season pack states a season and no episode ("Show.S01.1080p").
        # This is a complete name, not a silent film, so preserve the season.
        text = unicodedata.normalize("NFKC", str(media_name))
        text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
        for word in self._normalize_match_text(text).split():
            match = re.match(r"^s(\d{1,2})$", word, re.IGNORECASE)
            if match:
                return int(match.group(1)), None

        return None, None

    @staticmethod
    def normalize_media_name(value):
        """Drop apostrophes so queries match scene naming ("Widow's Bay" -> "Widows Bay")."""
        if not value:
            return value
        return APOSTROPHE_RE.sub("", str(value))

    @staticmethod
    def _normalize_match_text(value):
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
        text = APOSTROPHE_RE.sub("", text)
        text = fold_arabic(text)
        text = text.replace("_", " ").replace(".", " ")
        text = KEEP_MATCH_CHARS_RE.sub(" ", text)
        return " ".join(text.lower().split())

    # Daily/aired dates that should not be read as season/episode numbers.
    DAILY_DATE_RE = re.compile(
        r"\b(?:19|20)\d{2}[.\-](?:0[1-9]|1[0-2])[.\-](?:0[1-9]|[12]\d|3[01])\b"
    )

    @staticmethod
    def _episode_evidence(media_name, *, allow_bare=False):
        """Return (season, episode, confidence) without forcing uncertain data."""
        if not media_name:
            return None, None, "none"

        text = unicodedata.normalize("NFKC", str(media_name))
        text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
        if SubtitleUtils.DAILY_DATE_RE.search(text):
            return None, None, "none"
        explicit_patterns = (
            r"\b[Ss](\d{1,2})[\s._-]*[Ee](?:[Pp])?[\s._-]*(\d{1,3})\b",
            r"\b(\d{1,2})[xX](\d{1,3})\b",
            (
                r"\b[Ss]eason[\s._-]*(\d{1,2})"
                r"[\s._-]*(?:[Ee]pisode|[Ee]p|[Ee])[\s._-]*(\d{1,3})\b"
            ),
        )
        for pattern in explicit_patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1)), int(match.group(2)), "high"

        labeled = re.search(
            r"\b(?:episode|ep|round|e)[\s._#-]*(\d{1,3})\b",
            text,
            re.IGNORECASE,
        )
        if labeled:
            return None, int(labeled.group(1)), "medium"

        if allow_bare:
            bare = re.search(
                r"(?:^|\s+-\s+|\s+)(\d{1,3})"
                r"(?=\s*(?:end\b|\[[^\]]*\]|\([^)]*\)|$))",
                text,
                re.IGNORECASE,
            )
            if bare:
                return None, int(bare.group(1)), "low"

        return None, None, "none"

    @classmethod
    def _title_hypotheses(cls, media_name, *, allow_bare=False):
        if not media_name:
            return []

        text = unicodedata.normalize("NFKC", str(media_name))
        text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
        text = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", text)
        boundaries = (
            r"\b[Ss]\d{1,2}[\s._-]*[Ee](?:[Pp])?[\s._-]*\d{1,3}\b",
            r"\b\d{1,2}[xX]\d{1,3}\b",
            r"\b[Ss]eason[\s._-]*\d{1,2}\b",
            r"\b(?:episode|ep|round|e)[\s._#-]*\d{1,3}\b",
        )
        prefixes = [
            text[: match.start()]
            for pattern in boundaries
            if (match := re.search(pattern, text, re.IGNORECASE))
        ]
        if allow_bare:
            bare = re.search(
                r"(?:^|\s+-\s+|\s+)\d{1,3}"
                r"(?=\s*(?:end\b|\[[^\]]*\]|\([^)]*\)|$))",
                text,
                re.IGNORECASE,
            )
            if bare:
                prefixes.append(text[: bare.start()])
        if not prefixes:
            prefixes.append(text)

        technical_tokens = {
            "aac",
            "ac3",
            "amzn",
            "arabic",
            "atmos",
            "avc",
            "bluray",
            "ddp",
            "dl",
            "dts",
            "dvd",
            "eac3",
            "flac",
            "h264",
            "h265",
            "hdtv",
            "hdrip",
            "hevc",
            "netflix",
            "proper",
            "remux",
            "webrip",
            "web",
            "webdl",
            "x264",
            "x265",
        }
        hypotheses = []
        for prefix in prefixes:
            normalized = cls._normalize_match_text(prefix)
            normalized = re.sub(r"\b(?:19|20)\d{2}\b", " ", normalized)
            tokens = [
                token
                for token in normalized.split()
                if token not in technical_tokens
                and not re.fullmatch(r"\d{3,4}p", token)
                and not re.fullmatch(r"\d+bit", token)
                and not re.fullmatch(r"\d+(?:\.\d+)?ch", token)
            ]
            hypothesis = " ".join(tokens).strip()
            if hypothesis and hypothesis not in hypotheses:
                hypotheses.append(hypothesis)
        return hypotheses

    @staticmethod
    def _informative_title_tokens(title):
        stopwords = {
            "a",
            "an",
            "and",
            "at",
            "for",
            "from",
            "in",
            "of",
            "on",
            "the",
            "to",
        }
        return {token for token in title.split() if token not in stopwords}

    @classmethod
    def _title_match_score(cls, source_name, target_name, *, source_allow_bare=False):
        source_titles = cls._title_hypotheses(
            source_name,
            allow_bare=source_allow_bare,
        )
        target_titles = cls._title_hypotheses(target_name)
        best = 0.0
        fuzzy_matched = False
        for source_title in source_titles:
            source_tokens = cls._informative_title_tokens(source_title)
            if not source_tokens:
                continue
            for target_title in target_titles:
                target_tokens = cls._informative_title_tokens(target_title)
                if not target_tokens:
                    continue
                if source_tokens == target_tokens:
                    best = max(best, 55.0)
                    continue
                if source_tokens <= target_tokens or target_tokens <= source_tokens:
                    best = max(best, 53.0)
                    continue

                shared = source_tokens & target_tokens
                if len(shared) >= 2:
                    coverage = len(shared) / min(
                        len(source_tokens),
                        len(target_tokens),
                    )
                    best = max(best, 20.0 + 25.0 * coverage)

                similarity = fuzz.token_set_ratio(
                    " ".join(sorted(source_tokens)),
                    " ".join(sorted(target_tokens)),
                )
                if similarity >= 90:
                    best = max(best, 42.0)
                    fuzzy_matched = True
                elif similarity >= 80 and shared:
                    best = max(best, 30.0)
                    fuzzy_matched = True
        return _TitleMatch(score=min(best, 55.0), fuzzy_matched=fuzzy_matched)

    @classmethod
    def _technical_match_score(cls, source_name, target_name):
        terms = {
            "720p",
            "1080p",
            "2160p",
            "4k",
            "amzn",
            "bluray",
            "h264",
            "h265",
            "hdtv",
            "hdrip",
            "hevc",
            "netflix",
            "web",
            "webdl",
            "webrip",
            "x264",
            "x265",
        }
        source_tokens = set(cls._normalize_match_text(source_name).split())
        target_tokens = set(cls._normalize_match_text(target_name).split())
        return min(10.0, 2.0 * len(source_tokens & target_tokens & terms))

    # Arabic definite article. Stripping it turns "المشروع" into "مشروع", which is
    # how providers sometimes index the same title. Only applied when the remainder
    # is still a word worth searching for, and never to "الله".
    DEFINITE_ARTICLE = "ال"

    @classmethod
    def _title_variants(cls, title):
        """Every searchable spelling of one title: original, article-stripped, folded."""
        title = (title or "").strip()
        if not title:
            return []
        variants = [title]

        stripped_tokens = []
        for token in title.split():
            if (
                token.startswith(cls.DEFINITE_ARTICLE)
                and token != "الله"
                and len(token) >= 5
                and len(token) - len(cls.DEFINITE_ARTICLE) >= 3
            ):
                stripped_tokens.append(token[len(cls.DEFINITE_ARTICLE) :])
            else:
                stripped_tokens.append(token)
        stripped = " ".join(stripped_tokens)
        if stripped != title:
            variants.append(stripped)

        for variant in list(variants):
            folded = fold_arabic(variant)
            if folded != variant:
                variants.append(folded)

        variants.extend(cls._script_split_variants(title))
        return list(dict.fromkeys(variants))

    @staticmethod
    def _script_split_variants(title):
        """Split a mixed Arabic/Latin title into one variant per script."""
        latin = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.-]*", title))
        arabic = " ".join(re.findall(r"[؀-ۿ]+", title))
        if not latin or not arabic:
            return []
        return [latin, arabic]

    def get_alternate_names(self, media_name):
        """Generate alternate name formats for the media.

        Queries are built from clean title hypotheses so technical tokens
        (1080p, WEB-DL, x264, group names) never reach the provider.
        """
        try:
            if not media_name:
                return None

            season, episode = self.extract_season_and_episode(media_name)
            year = self._year_of(media_name)
            hypotheses = self._title_hypotheses(media_name, allow_bare=True)
            if not hypotheses:
                return None

            formats = []
            if episode is None:
                for title in hypotheses:
                    for variant in self._title_variants(title):
                        formats.append(variant)
                        if year:
                            formats.append(f"{variant} {year}")
                            formats.append(f"{variant} ({year})")
                return list(dict.fromkeys(formats))

            for title in hypotheses:
                for variant in self._title_variants(title):
                    formats.extend(
                        self._episode_formats(variant, year, season, episode)
                    )

            return list(dict.fromkeys(formats))
        except Exception as e:
            self.console.print(f"[bold red]Error generating alternate names: {e}[/]")
            return None

    @staticmethod
    def _episode_formats(title, year, season, episode):
        """Every episode-shaped spelling of one already-varied title."""
        formats = []
        if season:
            formats.extend(
                [
                    f"{title} {season}x{episode:02d}",
                    f"{title} S{season:02d}E{episode:02d}",
                    f"{title} Episode #{season}.{episode:02d}",
                ]
            )
        if year:
            formats.extend(
                [
                    f"{title} ({year}) - S{season:02d}E{episode:02d}",
                    f"{title} ({year}) {season}x{episode:02d}",
                ]
            )
        if season == 1:
            formats.extend(
                [
                    f"{title} E{episode:02d}",
                    f"{title.lower().replace(' ', '.')}.E{episode:02d}",
                ]
            )
        formats.append(f"{title.lower().replace(' ', '-')}-episode-{season}-{episode}")
        return formats

    def normalize_score(self, score):
        """
        Normalize subtitle matching score to 0-100 range

        Args:
            score (float): Raw score from matching algorithm

        Returns:
            float: Normalized score between 0-100
        """
        try:
            # Calculate actual max score based on current scoring system:
            # 100 (hash match)
            # + 55 (series name match)
            # + 45 (quality terms 9 × 5)
            # + Word matches (variable but capped implicitly)
            # + 100 (perfect fuzzy match)
            # + 50 (episode match)
            # + 25 (season match)
            # + 50 (quality indicators 5 × 10)
            # + 75 (perfect match bonus)
            MAX_POSSIBLE_SCORE = 500

            # Normalize using max possible score
            normalized = (score / MAX_POSSIBLE_SCORE) * 100

            # Clamp between 0-100
            return max(0, min(100, normalized))
        except Exception as e:
            self.console.print(f"[bold red]Error normalizing score: {e}[/]")
            return 0

    @staticmethod
    def _year_of(value):
        match = YEAR_RE.search(str(value or ""))
        return match.group(0) if match else ""

    @staticmethod
    def _release_reasons(release_name):
        """Source and resolution reasons, most specific source first."""
        text = str(release_name or "").lower()
        reasons = []
        for token, label in SOURCE_PATTERNS:
            if token in text:
                reasons.append(f"source: {label}")
                break
        resolution = RESOLUTION_RE.search(text)
        if resolution:
            reasons.append(f"resolution: {resolution.group(1)}")
        return reasons

    def explain_subtitle_match(
        self,
        subtitle_release_name,
        video_file_name,
        hash_match=False,
    ):
        """Score a release and report the evidence behind the number.

        ``score_subtitle`` is a thin wrapper over this, so the two can never disagree.
        Reasons are returned most significant first, capped at ``MAX_MATCH_REASONS``.
        Provider identity is deliberately absent: it is a tiebreak applied by the
        caller and never enters the score.
        """
        try:
            if not subtitle_release_name or not video_file_name:
                return MatchExplanation(score=0.0, reasons=())
            if hash_match:
                return MatchExplanation(
                    score=100.0,
                    reasons=("exact hash match",),
                )

            target_season, target_episode, target_confidence = (
                self._episode_evidence(video_file_name)
            )
            allow_bare = target_confidence in {"high", "medium"}
            source_season, source_episode, source_confidence = (
                self._episode_evidence(
                    subtitle_release_name,
                    allow_bare=allow_bare,
                )
            )
            title_match = self._title_match_score(
                subtitle_release_name,
                video_file_name,
                source_allow_bare=allow_bare,
            )
            title_score = title_match.score
            title_plausible = title_score >= 25
            score = title_score
            reasons = []

            episode_agrees = (
                source_episode is not None
                and target_episode is not None
                and source_episode == target_episode
            )
            if episode_agrees:
                episode_points = {
                    "high": 20.0,
                    "medium": 14.0,
                    "low": 8.0,
                }.get(source_confidence, 0.0)
                score += episode_points if title_plausible else min(6.0, episode_points)
                reasons.append("movie/episode match")
            elif (
                title_plausible
                and source_episode is not None
                and target_episode is not None
                and source_confidence in {"high", "medium"}
            ):
                score -= 15.0
            if source_episode is not None and target_episode is not None:
                if not episode_agrees:
                    reasons.append("episode mismatch")
            elif source_episode is None and target_episode is None:
                reasons.append("movie/episode match")

            if source_season is not None and target_season is not None:
                if source_season == target_season:
                    score += 10.0 if title_plausible else 3.0
                elif title_plausible and source_confidence == "high":
                    score -= 12.0
                if source_season != target_season:
                    reasons.append("season mismatch")

            if title_plausible and episode_agrees:
                score += 10.0 if source_confidence != "low" else 6.0

            technical_score = self._technical_match_score(
                subtitle_release_name,
                video_file_name,
            )
            score += technical_score if title_plausible else min(2.0, technical_score)
            if not title_plausible:
                score = min(score, 15.0)

            source_year = self._year_of(subtitle_release_name)
            target_year = self._year_of(video_file_name)
            if source_year and target_year:
                if source_year == target_year:
                    reasons.append(f"year match {source_year}")
                else:
                    reasons.append(f"year mismatch ({source_year})")
            if title_match.fuzzy_matched:
                reasons.append("release name match")
            reasons.extend(self._release_reasons(subtitle_release_name))

            return MatchExplanation(
                score=max(0.0, min(100.0, score)),
                reasons=tuple(reasons[:MAX_MATCH_REASONS]),
            )
        except Exception as e:
            self.console.print(f"[bold red]Error scoring subtitle: {e}[/]")
            return MatchExplanation(score=0.0, reasons=())

    def score_subtitle(self, subtitle_release_name, video_file_name, hash_match=False):
        """Score independent filename evidence without requiring a perfect parse."""
        return self.explain_subtitle_match(
            subtitle_release_name,
            video_file_name,
            hash_match,
        ).score

    def sort_subtitle_list(self, subtitles_list, scores=None):
        try:
            def _sort_key(x):
                if scores:
                    return scores.get(x.get("id"), 0)
                try:
                    return x["attributes"]["download_count"]
                except (KeyError, TypeError):
                    return 0

            sorted_subs = sorted(subtitles_list, key=_sort_key, reverse=True)

            return sorted_subs
        except Exception as e:
            self.console.print(f"[bold red]Unexpected error sorting subtitles: {e}[/]")
            return []

    def manual_select_subtitle(self, media_name, subtitles_list):
        try:
            if subtitles_list is None:
                return None

            scores = None
            if media_name:
                scores = {}
                for sub in subtitles_list:
                    release_name = sub["attributes"]["release"]
                    hash_match = sub["attributes"]["moviehash_match"]
                    score = self.score_subtitle(release_name, media_name, hash_match)
                    scores[sub["id"]] = score

            sorted_subs = self.sort_subtitle_list(subtitles_list, scores)
            self.display_subtitle_options_opensubtitle(sorted_subs, scores)

            while True:
                try:
                    choice = int(
                        self.console.input(
                            "[yellow]Enter the index of the subtitle you want to download (0 to cancel): [/yellow]"
                        )
                    )
                    if choice == 0:
                        return None
                    if 1 <= choice <= len(sorted_subs):
                        return sorted_subs[choice - 1]
                    self.console.print("[red]Invalid index. Please try again.[/red]")
                except ValueError:
                    self.console.print("[red]Please enter a valid number.[/red]")
        except Exception as e:
            self.console.print(f"[bold red]Error in manual subtitle selection: {e}[/]")
            return None

    def auto_select_subtitle(self, video_file_name, subtitles_result_list):
        try:
            max_score = -1
            best_subtitle = None
            scores = {}

            for subtitle in subtitles_result_list:
                release_name = subtitle["attributes"]["release"]
                hash_match = subtitle["attributes"]["moviehash_match"]
                score = self.score_subtitle(release_name, video_file_name, hash_match)
                scores[subtitle["id"]] = score

                if score > max_score:
                    max_score = score
                    best_subtitle = subtitle

            sorted_subs = self.sort_subtitle_list(subtitles_result_list, scores)
            self.display_subtitle_options_opensubtitle(sorted_subs, scores)
            return best_subtitle
        except Exception as e:
            self.console.print(f"[bold red]Error in auto subtitle selection: {e}[/]")
            return None

    def display_subtitle_options_opensubtitle(self, subtitles_list, scores=None):
        try:
            table = Table(title="Available Subtitles")

            table.add_column("Index", style="cyan", no_wrap=True)
            table.add_column("Subtitle ID", style="magenta")
            table.add_column("Release Name", style="magenta")
            table.add_column("Language", style="green")
            table.add_column("Downloads", style="yellow", justify="right")
            table.add_column("Hash Match", style="blue", justify="center")
            table.add_column("Machine Translated", style="red", justify="center")
            table.add_column("Auto Selection Score", style="cyan", justify="right")

            # Find max score if scores exist
            max_score = max(scores.values()) if scores else 0

            for idx, sub in enumerate(subtitles_list, start=1):
                attrs = sub["attributes"]
                sub_id = sub["id"]
                score = scores.get(sub_id, "") if scores else ""

                # Format score with color if it matches max score
                score_str = str(score) if score else "-"
                if score and score == max_score:
                    score_str = f"[green]{score_str}[/]"

                table.add_row(
                    str(idx),
                    sub_id,
                    attrs["release"],
                    attrs["language"],
                    str(attrs["download_count"]),
                    (
                        "[green]o[/]"
                        if attrs.get("moviehash_match", False)
                        else "[red]x[/]"
                    ),
                    "[green]o[/]" if attrs["machine_translated"] else "[red]x[/]",
                    score_str,
                )

            self.console.print(table)
        except Exception as e:
            self.console.print(f"[bold red]Error displaying subtitle options: {e}[/]")

    def ask_sync_subtitles(self):
        """Prompt user whether to sync subtitles"""
        while True:
            choice = self.console.input(
                "[yellow]Do you want to sync subtitles with video? (y/n): [/yellow]"
            ).lower()
            if choice in ["y", "yes"]:
                return True
            elif choice in ["n", "no"]:
                return False
            self.console.print("[red]Please enter y or n[/red]")

    def check_if_media_file(self, media_path):
        try:
            path = Path(media_path)
            if not path.exists():
                return False
            # if path is file
            if path.is_file() and path.suffix.lower() not in [
                ".mp4",
                ".mkv",
                ".avi",
            ]:
                return False
            return not path.is_dir()
        except Exception as e:
            self.console.print(f"[bold red]Error checking media file: {e}[/]")
            return False


if __name__ == "__main__":
    print("This is a module to be imported")
