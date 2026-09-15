import re
from pathlib import Path

import pytest

from library.OpenSubtitles import OpenSubtitles
from library.SubDL import SubDL
from library.SubSource import SubSource
from library.subtitle_utils import SubtitleUtils
from tui.config import (
    ApplicationConfig,
    CleaningConfig,
    GeneralConfig,
    ProviderConfig,
)
from tui.domain import Provider, SearchRequest, normalize_subtitle_format
from tui.providers.base import candidate_from_standardized
from tui.providers.factory import create_adapters
from tui.providers.opensubtitles import OpenSubtitlesAdapter
from tui.providers.subdl import SubDLAdapter
from tui.providers.subsource import SubSourceAdapter

REQUEST = SearchRequest(
    media_path=Path("Movie.mkv"),
    query="Movie",
    language="en",
)
SUBDL_RESPONSE = {
    "id": "77",
    "attributes": {
        "release": "Movie.2026.1080p",
        "language": "en",
        "url": "https://dl.example/file.zip?api_key=secret",
        "download_count": 5,
    },
}
SUBSOURCE_RESPONSE = {
    "id": "88",
    "attributes": {
        "release": "Movie WEB",
        "language": "english",
        "download_count": 9,
    },
}


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def search_candidates(self, path, language, query):
        return self.rows


class FailingClient:
    def search_candidates(self, path, language, query):
        raise OSError("network unavailable")


class SecretFailingClient:
    def search_candidates(self, path, language, query):
        raise OSError("GET https://example.test/search?api_key=do-not-leak&lang=en")


class QuietConsole:
    def print(self, *_args, **_kwargs):
        return None


def test_factory_requests_inclusive_opensubtitles_results(monkeypatch):
    monkeypatch.setattr(OpenSubtitles, "login", lambda _self: "token")
    providers = {provider: ProviderConfig(provider) for provider in Provider}
    providers[Provider.OPENSUBTITLES].values.update(
        username="user",
        password="pass",
        api_key="key",
        user_agent="app",
    )
    config = ApplicationConfig(
        general=GeneralConfig(),
        providers=providers,
        cleaning=CleaningConfig(),
    )

    adapter = create_adapters(config)[Provider.OPENSUBTITLES]

    assert adapter.client.hearing_impaired is True


def test_opensubtitles_search_requests_inclusive_hi_results(monkeypatch):
    captured = {}
    client = object.__new__(OpenSubtitles)
    client.hearing_impaired = True
    client.api_key = "key"
    client.token = "token"
    client.user_agent = "app"
    client.console = QuietConsole()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    def fake_get(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("library.OpenSubtitles.requests.get", fake_get)

    client.search(media_name="Movie", languages="en")

    assert captured["params"]["hearing_impaired"] == "include"


def test_provider_translation_and_hi_flags_are_normalized():
    opensubtitles = candidate_from_standardized(
        Provider.OPENSUBTITLES,
        {
            "id": "1",
            "attributes": {
                "hearing_impaired": True,
                "machine_translated": True,
            },
        },
    )
    subdl = candidate_from_standardized(
        Provider.SUBDL,
        {"id": "2", "attributes": {"hi": True}},
    )

    assert opensubtitles.hearing_impaired is True
    assert opensubtitles.ai_translated is True
    assert subdl.hearing_impaired is True
    assert subdl.ai_translated is False


def test_subdl_adapter_keeps_authenticated_url_private():
    adapter = SubDLAdapter(client=FakeClient([SUBDL_RESPONSE]))

    result = adapter.search(REQUEST)
    candidate = result.candidates[0]

    assert candidate.provider is Provider.SUBDL
    assert candidate.key.startswith("subdl:")
    assert "api_key" not in str(candidate.as_public_dict())
    assert candidate.public_url is None
    assert candidate.download_ref["attributes"]["url"].endswith("api_key=secret")


def test_subsource_adapter_normalizes_language_to_code():
    adapter = SubSourceAdapter(client=FakeClient([SUBSOURCE_RESPONSE]))

    result = adapter.search(REQUEST)

    assert result.candidates[0].language == "en"


def test_provider_error_is_distinct_from_zero_results():
    failed = OpenSubtitlesAdapter(client=FailingClient()).search(REQUEST)
    empty = OpenSubtitlesAdapter(client=FakeClient([])).search(REQUEST)

    assert failed.error is not None
    assert empty.error is None
    assert empty.candidates == []


def test_provider_errors_redact_authenticated_urls():
    failed = SubDLAdapter(client=SecretFailingClient()).search(REQUEST)

    assert "do-not-leak" not in failed.error
    assert "api_key=[redacted]" in failed.error


def test_missing_provider_id_gets_stable_source_scoped_fingerprint():
    row = {"attributes": {"release": "Same", "language": "English"}}
    first = OpenSubtitlesAdapter(client=FakeClient([row])).search(REQUEST)
    second = OpenSubtitlesAdapter(client=FakeClient([row])).search(REQUEST)

    assert first.candidates[0].key == second.candidates[0].key
    assert first.candidates[0].key.startswith("opensubtitles:fingerprint-")


def test_opensubtitles_candidates_add_hash_and_filename_results(tmp_path):
    media = tmp_path / "Dune - Prophecy (2024) - S01E01.mkv"
    media.touch()
    client = object.__new__(OpenSubtitles)
    client.subtitle_utils = type(
        "SearchUtils",
        (),
        {
            "hashFile": staticmethod(lambda _path: "movie-hash"),
            "normalize_media_name": staticmethod(SubtitleUtils.normalize_media_name),
            "get_alternate_names": staticmethod(
                lambda _name: ["Dune Prophecy S01E01", "Dune.Prophecy.1x01"]
            ),
        },
    )()
    calls = []

    def search(*, media_hash, media_name, languages):
        calls.append((media_hash, media_name, languages))
        is_hash_search = bool(media_hash)
        return [
            {
                "id": f"id-{len(calls)}",
                "attributes": {
                    "release": media_name or "hash release",
                    "moviehash_match": is_hash_search,
                },
            },
            {
                "id": "shared",
                "attributes": {
                    "release": "duplicate",
                    "moviehash_match": is_hash_search,
                },
            },
        ]

    client.search = search

    results = client.search_candidates(media, "en", media.stem)

    assert calls[0] == ("movie-hash", "", "en")
    assert [call[1] for call in calls[1:]] == [
        media.stem,
        "Dune - Prophecy (2024)",
        "Dune Prophecy S01E01",
        "Dune.Prophecy.1x01",
    ]
    assert all(call[0] == "" for call in calls[1:])
    assert len(results) == 6
    shared = next(row for row in results if row["id"] == "shared")
    assert shared["attributes"]["moviehash_match"] is True


def test_opensubtitles_search_queries_drop_apostrophes(tmp_path):
    media = tmp_path / "Widow's Bay (2026) - S01E01 - - Welcome.mkv"
    media.touch()
    client = object.__new__(OpenSubtitles)
    client.subtitle_utils = type(
        "SearchUtils",
        (),
        {
            "hashFile": staticmethod(lambda _path: ""),
            "normalize_media_name": staticmethod(SubtitleUtils.normalize_media_name),
            "get_alternate_names": staticmethod(lambda _name: []),
        },
    )()
    queries = []

    def search(*, media_hash, media_name, languages):
        if media_name:
            queries.append(media_name)
        return []

    client.search = search

    client.search_candidates(media, "en", media.stem)

    assert queries[0] == "Widows Bay (2026) - S01E01 - - Welcome"
    assert "Widows Bay (2026)" in queries
    assert all("'" not in name for name in queries)


def test_subdl_search_queries_drop_apostrophes(tmp_path):
    media = tmp_path / "Widow's Bay (2026) - S01E01.mkv"
    client = object.__new__(SubDL)
    client.console = QuietConsole()
    client.hearing_impaired = False
    client.subtitle_utils = SubtitleUtils()
    queries = []

    class EmptyResult:
        subtitles = []
        metadata_results = []

    client.filename_search = lambda *, filename, **_kwargs: (
        queries.append(filename),
        EmptyResult(),
    )[1]

    def search(**kwargs):
        for kind in ("file_name", "film_name"):
            if kwargs.get(kind):
                queries.append(kwargs[kind])
        return EmptyResult()

    client.search = search
    client.movie_search = lambda q, **_kwargs: queries.append(q) or []

    client._gather_candidates(media, "en")

    assert "Widows Bay (2026)" in queries
    assert all("'" not in name for name in queries)


def test_subsource_search_queries_drop_apostrophes(tmp_path):
    media = tmp_path / "Widow's Bay (2026) - S01E01.mkv"
    client = object.__new__(SubSource)
    client.console = QuietConsole()
    client.hearing_impaired = False
    client.subtitle_utils = SubtitleUtils()
    queries = []
    client.movie_search = lambda query="", **_kwargs: queries.append(query) or []

    client._gather_candidates(media, "en")

    # The primary query for the filename, with the apostrophe dropped. When nothing
    # resolves, the alternate-name fallback may add further variant queries; what
    # this test guards is the apostrophe handling on every one of them.
    assert queries[0] == "Widows Bay (2026)"
    assert all("'" not in name for name in queries)


def test_opensubtitles_legacy_download_uses_external_output_directory(tmp_path):
    output_directory = tmp_path / "subtitles"
    media = tmp_path / "library" / "Movie.mkv"
    saved_paths = []
    client = object.__new__(OpenSubtitles)
    client.output_directory = output_directory
    client.auto_select = True
    client.sync_audio_to_subs = False
    client.console = QuietConsole()
    client._gather_candidates = lambda *_args: ([{"id": "1"}], False)
    client.get_download_link = lambda _selected: "https://example.test/subtitle"
    client.print_subtitle_info = lambda _selected: None

    def save_subtitle(_url, path):
        saved_paths.append(path)
        return True

    client.save_subtitle = save_subtitle
    client.subtitle_utils = type(
        "Utils",
        (),
        {
            "sort_list_of_dicts_by_key": staticmethod(lambda rows, _key: rows),
            "auto_select_subtitle": staticmethod(lambda _name, rows: rows[0]),
            "clean_subtitles": staticmethod(lambda _path: None),
        },
    )()

    assert client.process_media_file(media, "en") is True
    assert saved_paths == [output_directory / "Movie.en.srt"]


def test_subdl_legacy_single_file_uses_external_output_directory(
    tmp_path,
    monkeypatch,
):
    output_directory = tmp_path / "subtitles"
    media = tmp_path / "library" / "Movie.mkv"
    client = object.__new__(SubDL)
    client.output_directory = output_directory
    client.download_base_url = "https://example.test"
    client.console = QuietConsole()
    response = type(
        "Response",
        (),
        {
            "content": b"subtitle",
            "raise_for_status": staticmethod(lambda: None),
        },
    )()
    monkeypatch.setattr(
        "library.SubDL.requests.get",
        lambda *_args, **_kwargs: response,
    )

    result = client._download_single_file("/movie.srt", "srt", media, "en")

    assert result == output_directory / "Movie.en.srt"
    assert result.read_text(encoding="utf-8") == "subtitle"


def test_subsource_legacy_archive_uses_external_output_directory(tmp_path):
    output_directory = tmp_path / "subtitles"
    media = tmp_path / "library" / "Movie.mkv"
    archive_paths = []
    client = object.__new__(SubSource)
    client.output_directory = output_directory
    client.console = QuietConsole()
    client._download_url_for = lambda _subtitle_id: "https://example.test/archive"
    client._get_raw = lambda _url: type(
        "Response",
        (),
        {"iter_content": staticmethod(lambda chunk_size: [b"archive"])},
    )()

    class FakeArchive:
        def names(self):
            return ["release.srt"]

        def read(self, _name):
            return b"subtitle"

        def close(self):
            return None

    def open_archive(path):
        archive_paths.append(path)
        return FakeArchive()

    client._open_archive = open_archive

    result = client._download_archive(
        {"id": "88"},
        media,
        "en",
        None,
        None,
        True,
    )

    assert archive_paths == [output_directory / "Movie.download"]
    assert result == output_directory / "Movie.en.srt"
    assert result.read_text(encoding="utf-8") == "subtitle"


def test_subdl_legacy_external_output_does_not_overwrite_existing_file(
    tmp_path,
    monkeypatch,
):
    output_directory = tmp_path / "subtitles"
    output_directory.mkdir()
    existing = output_directory / "Movie.en.srt"
    existing.write_text("old", encoding="utf-8")
    client = object.__new__(SubDL)
    client.output_directory = output_directory
    client.download_base_url = "https://example.test"
    client.console = QuietConsole()
    response = type(
        "Response",
        (),
        {
            "content": b"new",
            "raise_for_status": staticmethod(lambda: None),
        },
    )()
    monkeypatch.setattr(
        "library.SubDL.requests.get",
        lambda *_args, **_kwargs: response,
    )

    result = client._download_single_file(
        "/movie.srt",
        "srt",
        tmp_path / "library" / "Movie.mkv",
        "en",
    )

    assert result is None
    assert existing.read_text(encoding="utf-8") == "old"


def test_opensubtitles_legacy_external_output_does_not_overwrite_existing_file(
    tmp_path,
):
    output_directory = tmp_path / "subtitles"
    output_directory.mkdir()
    existing = output_directory / "Movie.en.srt"
    existing.write_text("old", encoding="utf-8")
    client = object.__new__(OpenSubtitles)
    client.output_directory = output_directory
    client.auto_select = True
    client.sync_audio_to_subs = False
    client.console = QuietConsole()
    client._gather_candidates = lambda *_args: ([{"id": "1"}], False)
    client.get_download_link = lambda _selected: "https://example.test/subtitle"
    client.print_subtitle_info = lambda _selected: None
    client.save_subtitle = lambda _url, path: path.write_text(
        "new",
        encoding="utf-8",
    )
    client.subtitle_utils = type(
        "Utils",
        (),
        {
            "sort_list_of_dicts_by_key": staticmethod(lambda rows, _key: rows),
            "auto_select_subtitle": staticmethod(lambda _name, rows: rows[0]),
            "clean_subtitles": staticmethod(lambda _path: None),
        },
    )()

    result = client.process_media_file(
        tmp_path / "library" / "Movie.mkv",
        "en",
    )

    assert result is False
    assert existing.read_text(encoding="utf-8") == "old"


def test_subsource_legacy_external_output_does_not_overwrite_existing_file(
    tmp_path,
):
    output_directory = tmp_path / "subtitles"
    output_directory.mkdir()
    existing = output_directory / "Movie.en.srt"
    existing.write_text("old", encoding="utf-8")
    client = object.__new__(SubSource)
    client.output_directory = output_directory
    client.console = QuietConsole()
    client._download_url_for = lambda _subtitle_id: "https://example.test/archive"
    client._get_raw = lambda _url: type(
        "Response",
        (),
        {"iter_content": staticmethod(lambda chunk_size: [b"archive"])},
    )()

    class FakeArchive:
        def names(self):
            return ["release.srt"]

        def read(self, _name):
            return b"new"

        def close(self):
            return None

    client._open_archive = lambda _path: FakeArchive()

    result = client._download_archive(
        {"id": "88"},
        tmp_path / "library" / "Movie.mkv",
        "en",
        None,
        None,
        True,
    )

    assert result is None
    assert existing.read_text(encoding="utf-8") == "old"


# --- Alternate-name query variants ----------------------------------------

MOVIE_FILENAME = "The.Pitt.2025.1080p.WEB-DL.x264-FLUX"


def _has_arabic(text):
    return bool(re.search(r"[؀-ۿ]", text))


def test_get_alternate_names_generates_variants_for_a_movie():
    # Regression: this returned None for any name without an episode number, so
    # films received no alternate queries at all.
    names = SubtitleUtils().get_alternate_names(MOVIE_FILENAME)

    assert names
    assert any("Pitt" in name for name in names)


def test_get_alternate_names_movie_variants_include_the_year():
    names = SubtitleUtils().get_alternate_names(MOVIE_FILENAME)

    assert any("2025" in name for name in names)


def test_get_alternate_names_keeps_the_original_title_as_a_variant():
    names = SubtitleUtils().get_alternate_names(MOVIE_FILENAME)

    # The filename's own title spelling leads the list: no article stripping, no
    # folding, and no script split is allowed to displace it.
    assert names[0].startswith("The.Pitt")


def test_get_alternate_names_still_handles_episodes():
    names = SubtitleUtils().get_alternate_names("The.Pitt.S01E01.1080p.WEB-DL")

    assert names
    assert any("S01E01" in name for name in names)


def test_arabic_variant_is_additive_and_folded():
    # The trailing teh marbuta (U+0629) folds to heh (U+0647), so the folded
    # spelling is a genuinely different query string from the one on the filename.
    # Providers index both, so both must be searched.
    names = SubtitleUtils().get_alternate_names("الهيبة.S01E03.1080p.WEB-DL")

    assert names
    assert any("الهيبه" in name for name in names)


def test_definite_article_is_stripped_from_a_long_token():
    variants = SubtitleUtils._title_variants("المشروع")

    assert "المشروع" in variants
    assert "مشروع" in variants


def test_definite_article_is_not_stripped_from_short_tokens():
    # Boundary: a five-character token loses its article, a four-character one
    # does not, because the remainder would be too short to be worth searching for.
    five_char = SubtitleUtils._title_variants("البيت")
    four_char = SubtitleUtils._title_variants("الحي")

    assert "بيت" in five_char
    assert four_char == ["الحي"]


def test_definite_article_is_never_stripped_from_allah():
    variants = SubtitleUtils._title_variants("الله")

    assert "الله" in variants
    # "الله" is four characters, so it is already protected by the length rule;
    # stripping its article would yield "له".
    assert "له" not in variants


def test_mixed_script_stem_produces_one_variant_per_script():
    variants = SubtitleUtils._title_variants("Al-Hayba.الهيبة.1080p")
    names = SubtitleUtils().get_alternate_names("Al-Hayba.الهيبة.S01E03.1080p")

    assert "الهيبة" in variants
    assert any(
        "Al-Hayba" in variant and not _has_arabic(variant) for variant in variants
    )
    assert names
    assert any("الهيبة" in name for name in names)
    assert any("Al-Hayba" in name or "Al Hayba" in name for name in names)


# --- Format detection at the provider boundary ----------------------------

@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"attributes": {"sub_format": "ass"}}, "ass"),
        ({"attributes": {"format": "SSA"}}, "ssa"),
        ({"attributes": {"url": "https://x.test/a/b/file.vtt"}}, "vtt"),
        ({"attributes": {"url": "https://x.test/a/file.SRT?api_key=abc"}}, "srt"),
        ({"attributes": {"url": "https://x.test/a/file.zip"}}, None),
        ({"attributes": {}}, None),
    ],
)
def test_candidate_format_is_populated_from_provider_metadata(row, expected):
    candidate = candidate_from_standardized(Provider.SUBDL, row)

    assert candidate.format == expected


def test_candidate_format_prefers_sub_format_over_url():
    candidate = candidate_from_standardized(
        Provider.SUBDL,
        {"attributes": {"sub_format": "ass", "url": "https://x.test/a.srt"}},
    )

    assert candidate.format == "ass"


@pytest.mark.parametrize("extension", ["srt", "ass", "ssa", "vtt", "sub"])
def test_candidate_format_is_read_from_unpacked_archive_members(extension):
    # SubDL answers with an archive at attributes["url"] and lists the subtitle
    # files inside it under attributes["unpack_files"]. The archive URL is a
    # ".zip", which normalizes to no format at all, so every SubDL row rendered a
    # blank Fmt cell until the archive listing was consulted.
    candidate = candidate_from_standardized(
        Provider.SUBDL,
        {
            "attributes": {
                "url": "https://dl.subdl.com/subtitle/1234-abcd/Amadeus.1984.zip",
                "unpack_files": [
                    {
                        "url": (
                            "https://dl.subdl.com/subtitle/1234-abcd/"
                            f"Amadeus.1984.{extension}"
                        ),
                        "format": extension,
                        "season": None,
                        "episode": None,
                    }
                ],
            }
        },
    )

    assert candidate.format == extension


def test_candidate_format_uses_an_archive_member_extension_without_a_format_key():
    candidate = candidate_from_standardized(
        Provider.SUBDL,
        {
            "attributes": {
                "url": "https://dl.subdl.com/subtitle/1234-abcd/Amadeus.1984.zip",
                "unpack_files": [
                    {"url": "https://dl.subdl.com/subtitle/1234-abcd/bundle.ass"}
                ],
            }
        },
    )

    assert candidate.format == "ass"


def test_candidate_format_prefers_provider_metadata_over_archive_members():
    # An explicit format the provider states about the result itself outranks the
    # listing of what happens to be inside its archive.
    candidate = candidate_from_standardized(
        Provider.SUBDL,
        {
            "attributes": {
                "sub_format": "ass",
                "url": "https://x.test/a/bundle.zip",
                "unpack_files": [{"url": "https://x.test/a.srt", "format": "srt"}],
            }
        },
    )

    assert candidate.format == "ass"


def test_candidate_format_ignores_archive_members_it_cannot_recognize():
    candidate = candidate_from_standardized(
        Provider.SUBDL,
        {
            "attributes": {
                "url": "https://x.test/a/bundle.zip",
                "unpack_files": [{"url": "https://x.test/a.mkv", "format": "mkv"}],
            }
        },
    )

    assert candidate.format is None


@pytest.mark.parametrize(
    "unpack_files",
    [None, "srt", 7, [None], [{}], [{"url": ""}]],
)
def test_candidate_format_survives_unusable_archive_listings(unpack_files):
    # unpack_files is provider-supplied, so an unexpected shape must degrade to
    # "no format" rather than take the whole provider search down with it.
    candidate = candidate_from_standardized(
        Provider.SUBDL,
        {
            "attributes": {
                "url": "https://x.test/a/bundle.zip",
                "unpack_files": unpack_files,
            }
        },
    )

    assert candidate.format is None


def test_normalize_subtitle_format_is_total():
    assert normalize_subtitle_format(None) is None
    assert normalize_subtitle_format("") is None
    assert normalize_subtitle_format("   ") is None
    assert normalize_subtitle_format("mkv") is None


# --- Legacy CLI format preservation ---------------------------------------

class _FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _SilentConsole:
    def print(self, *args, **kwargs):
        return None


def test_subdl_single_file_download_keeps_an_ssa_extension(tmp_path, monkeypatch):
    import library.SubDL as subdl_module

    monkeypatch.setattr(
        subdl_module.requests,
        "get",
        lambda *a, **k: _FakeResponse(b"[Script Info]\n"),
    )
    client = object.__new__(subdl_module.SubDL)
    client.output_directory = None
    client.console = _SilentConsole()

    written = subdl_module.SubDL._download_single_file(
        client,
        "https://x.test/file.zip",
        "ssa",
        tmp_path / "Movie.2026.mkv",
        "ar",
    )

    assert written is not None
    assert written.suffix == ".ssa"


def test_subdl_zip_scan_recognizes_ssa_members():
    import inspect

    import library.SubDL as subdl_module

    source = inspect.getsource(subdl_module.SubDL._download_zip)

    assert '".ssa"' in source


def test_subdl_zip_extracts_an_ssa_member_as_ssa(tmp_path, monkeypatch):
    import io
    import zipfile

    import library.SubDL as subdl_module

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Movie.2026.ar.ssa", "[Script Info]\n")
    payload = archive.getvalue()

    class StreamingResponse:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            return [payload]

    monkeypatch.setattr(subdl_module.requests, "get", lambda *a, **k: StreamingResponse())
    client = object.__new__(subdl_module.SubDL)
    client.output_directory = None
    client.console = _SilentConsole()

    written = subdl_module.SubDL._download_zip(
        client,
        "https://x.test/file.zip",
        tmp_path / "Movie.2026.mkv",
        "ar",
        None,
        None,
        True,
    )

    assert written is not None
    assert written.name == "Movie.2026.ar.ssa"
    assert written.read_text(encoding="utf-8") == "[Script Info]\n"


def test_subdl_zip_extracts_an_srt_member_as_srt(tmp_path, monkeypatch):
    import io
    import zipfile

    import library.SubDL as subdl_module

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Movie.2026.ar.srt", "1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    payload = archive.getvalue()

    class StreamingResponse:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            return [payload]

    monkeypatch.setattr(subdl_module.requests, "get", lambda *a, **k: StreamingResponse())
    client = object.__new__(subdl_module.SubDL)
    client.output_directory = None
    client.console = _SilentConsole()

    written = subdl_module.SubDL._download_zip(
        client,
        "https://x.test/file.zip",
        tmp_path / "Movie.2026.mkv",
        "ar",
        None,
        None,
        True,
    )

    assert written is not None
    assert written.name == "Movie.2026.ar.srt"
