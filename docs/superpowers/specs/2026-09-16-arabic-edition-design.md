# Arabic Edition — Phase 1 Design

Arabic-first subtitle discovery and download. The application should become
excellent at finding and downloading existing Arabic subtitles, rather than
translating them or localizing its own interface.

## Goal

Make Arabic the default target language and make discovery of existing Arabic
subtitles accurate: correct movie/episode identification, Arabic-aware matching,
alternate-title query variants, ranking that prefers the best Arabic match over
the first one, and format preservation so ASS/SSA subtitles are not downgraded
to SRT.

Phase 1 adds no AI translation, no UI localization, no RTL work, and no new
providers or metadata sources.

## Scope

**In scope**

- Arabic as the default target language, with English reachable as an explicit
  fallback.
- Arabic text folding for matching.
- Arabic-aware query variants, including a fix for movies producing none.
- Ranking by language, match score, hash match, provider reliability, and
  download count.
- Human-readable match reasons surfaced in the UI.
- ASS/SSA/VTT format preservation end to end.
- Sync disabled by default.

**Out of scope (deferred to later phases)**

- AI Arabic translation as a fallback when no Arabic subtitle exists.
- Modern GUI / drag-and-drop.
- Arabic UI localization and RTL rendering.
- Any metadata source (TMDB, IMDb, TVDB) for true alternate titles.
- New subtitle providers.
- Redesigning any existing screen or layout.

## Language defaults and precedence

The existing precedence from
`docs/superpowers/specs/2026-07-30-automatic-language-selection-design.md`
is unchanged:

1. `--lang CODE`
2. `general.default_language`
3. The first configured provider language.

Changed default: `GeneralConfig.default_language` moves from `""` to `"ar"`
(`tui/config.py:37`), and `config.yaml.sample` ships `default_language: "ar"`.
A user who wants English restores it with `--lang en` or by setting
`general.default_language: en`.

The provider language mappings already contain `Arabic: ar` for all three
providers and are not modified.

`SessionState.language` (`tui/state.py:297`) keeps its `"en"` dataclass default
deliberately. It is superseded at startup by language resolution in
`download_subs.py` and `SubsApp._initial_language()` (`tui/app.py:651-664`), so
changing it would have no runtime effect and would only create a second,
contradictory source of truth.

## Arabic text folding

Add module-level constants and fold them into `SubtitleUtils._normalize_match_text`
(`library/subtitle_utils.py:336`), following the pattern established for
`APOSTROPHE_RE` by commit `216ec9c`.

Order of operations, applied after the existing NFKC normalization:

1. Remove Arabic combining marks: `U+064B–U+065F` and `U+0670` (harakat and
   superscript alef), and `U+06D6–U+06ED` (Quranic annotation signs).
2. Remove tatweel `U+0640`.
3. Fold alef forms to bare alef `U+0627`: `U+0622` آ, `U+0623` أ, `U+0625` إ,
   `U+0671` ٱ.
4. Fold teh marbuta `U+0629` ة to `U+0647` ه.
5. Fold alef maksura `U+0649` ى to `U+064A` ي.
6. Fold Arabic-Indic digits `U+0660–U+0669` and Extended Arabic-Indic digits
   `U+06F0–U+06F9` to ASCII `0–9`.

`U+0624` ؤ and `U+0626` ئ are separate letters and are not folded. Arabic
presentation forms (`U+FB50–U+FDFF`, `U+FE70–U+FEFF`) are already normalized to
their base letters by the existing NFKC step and need no additional handling.

**Folding applies to the matching side only.** It is deliberately not added to
`normalize_media_name` (`library/subtitle_utils.py:329`), because that function
produces the text sent to providers as a search query. Providers index real
titles; folding the query would remove matches rather than add them. Query-side
variation is handled by generative variants instead (next section).

These functions must be total: empty, `None`, and Latin-only input pass through
unchanged and never raise.

## Query variant generation

`SubtitleUtils.get_alternate_names` (`library/subtitle_utils.py:544`) becomes the
single choke point through which every provider generates query variants. It is
already called by OpenSubtitles (`library/OpenSubtitles.py:142`) and SubDL
(`library/SubDL.py:220`); SubSource does not call it today and will be updated to
do so.

Two changes:

**Fix the movie gap.** `library/subtitle_utils.py:552-553` currently returns
`None` whenever no episode number is parsed, so films receive no alternate names
at all. Movies will instead generate title-only variants: the bare title,
`"{title} {year}"`, and `"{title} ({year})"`, each included only when a year was
parsed.

**Generate Arabic title variants.** The existing format-generation block is
driven once per title variant rather than once per title:

- the extracted title itself;
- the definite-article form with a leading `ال` removed, added only when the
  original token is at least 5 characters and the remainder is at least 3
  characters, and skipped for the token `الله`;
- the folded form of each of the above, using the folding rules above;
- when the filename stem contains both an Arabic run and a Latin run, each
  script's title as its own variant.

Every variant is additive. The original title is always present, so existing
output for non-Arabic media is unchanged.

This function remains the seam for a future metadata source. A TMDB-style lookup
can later contribute titles to the same list without any provider change, which
is why no separate abstraction layer is introduced now.

## Provider coverage

The requirement to search every provider offering Arabic is met by the
`all-providers` engine mode, which already fan-outs to all configured adapters
in parallel (`tui/search.py:67-106`, `ThreadPoolExecutor` at `:77`). Making
`preferred_backend` default to `EngineMode.ALL_PROVIDERS` is therefore
load-bearing rather than cosmetic: `auto()` is a *sequential* fallback that
returns as soon as the first provider yields any candidates
(`tui/search.py:139-145`), so under `auto` a mediocre SubSource result would
prevent OpenSubtitles and SubDL from being consulted at all. `auto` remains
available and unchanged for users who want that behaviour.

No change is made to which providers are queried, and no provider is added.

## Ranking

`SearchCoordinator._prepare` (`tui/search.py:162-196`) currently sorts by:

```python
(item.score, item.hash_match, item.download_count, -PROVIDER_PRIORITY[item.provider])
```

The new sort key is:

```python
(_language_priority, item.score, item.hash_match, provider_reliability, item.download_count)
```

- `_language_priority` is `1` when the candidate language equals the requested
  language, else `0`. This is a defensive guarantee that a non-target-language
  candidate can never outrank a target-language one in `all-providers` mode.
- `provider_reliability` is derived from the existing `AUTO_PRIORITY`
  (`tui/search.py:19-23`) as `len(AUTO_PRIORITY) - index`, giving SubSource 3,
  OpenSubtitles 2, SubDL 1. Deriving it from `AUTO_PRIORITY` keeps the two from
  drifting apart.

`PROVIDER_PRIORITY` (`tui/search.py:24`) is **retained unchanged**. It is not
interchangeable with `provider_reliability` in intent even though the two are
inverses: `PROVIDER_PRIORITY` orders the sequential provider attempts in `auto()`
(`tui/search.py:120-126`), whereas reliability ranks candidates that have
already been gathered. Removing it would break `auto()`. The sort key stops
using it; the `auto()` usage stays. A comment at the definition should record
that they are inverses of one another.

**Deliberate decision:** provider reliability is placed after `score` and
`hash_match` in the sort key rather than being added into the score itself. An
exact movie-hash match is a stronger correctness signal than provider identity,
and keeping `score_subtitle` provider-agnostic preserves its current contract and
existing test semantics. The effect is that provider reliability breaks ties
among equally-scored, equally-hash-matched candidates, ahead of download count.
It is additionally surfaced in match reasons so the influence is visible.

## Match reasons

Add a frozen dataclass:

```python
@dataclass(frozen=True)
class MatchExplanation:
    score: float
    reasons: tuple[str, ...]
```

Add `SubtitleUtils.explain_subtitle_match(release, query, hash_match=False) ->
MatchExplanation` (`library/subtitle_utils.py`). `score_subtitle`
(`library/subtitle_utils.py:693`) becomes a thin wrapper returning
`explain_subtitle_match(...).score`, so its signature, return type, and numeric
behavior are unchanged and all existing assertions in
`tui/tests/test_scoring.py` continue to pass.

The reason set for Phase 1 is closed. Reasons are emitted in the order below and
truncated to the first six, so the least significant evidence is dropped first
and the episode determination and provider are never hidden:

| # | Reason | Emitted when |
|---|---|---|
| 1 | `exact hash match` | `hash_match` is true |
| 2 | `movie/episode match` | Release and query agree on the episode, or both carry none |
| 3 | `episode mismatch` | Query and release both name an episode and they differ |
| 4 | `season mismatch` | Query and release both name a season and they differ |
| 5 | `year match {year}` | Both name a year and they are equal |
| 6 | `year mismatch ({year})` | Both name a year and they differ |
| 7 | `release name match` | Fuzzy title ratio meets the existing threshold |
| 8 | `provider: {label} (reliability {n}/3)` | Always emitted |
| 9 | `source: {SOURCE}` | A release source token is detected, such as `WEB-DL` |
| 10 | `resolution: {RES}` | A resolution token is detected, such as `1080p` |

Adding a reason beyond this table requires adding a test for it.

`SearchCoordinator._prepare` (`tui/search.py:178-186`) populates
`candidate.match_reasons`. It calls `explain_subtitle_match` when the injected
scorer provides it and falls back to `score_subtitle` with an empty reason tuple
otherwise, so the existing duck-typed test scorers keep working unchanged.

New field: `Candidate.match_reasons: tuple[str, ...] = ()`
(`tui/domain.py:57-71`), included in `as_public_dict()` (`tui/domain.py:77-91`).

## Format preservation

`Candidate` gains `format: str | None = None` (`tui/domain.py:57-71`), also
included in `as_public_dict()`. It is populated in `candidate_from_standardized`
(`tui/providers/base.py:89-132`) from `attributes.get("sub_format")` or
`attributes.get("format")` or the extension of the provider URL or file name,
lowercased and validated against `{srt, ass, ssa, vtt, sub}`.

Because provider metadata is not always present, `JobCoordinator._stage_download`
(`tui/jobs.py:118-159`) additionally sniffs the staged file's content when
`candidate.format` is unknown, and renames within the staging directory before
`os.replace`:

- contains `[V4+ Styles]` or `[Script Info]` → `.ass`
- contains `[V4 Styles]` → `.ssa`
- otherwise → `.srt`

Sniffing happens only inside the existing `TemporaryDirectory`, so a failed or
ambiguous detection cannot leave a partial or misnamed file beside the media.

Required fixes:

- `tui/providers/opensubtitles.py:16` currently forces `.srt` on the staging
  path, so the extension is derived from `candidate.format` when known and left
  to the sniffer otherwise. This is the only adapter that hardcodes an
  extension; `tui/providers/subdl.py:17` and `tui/providers/subsource.py:17`
  already return the path their client produced.
- `tui/jobs.py:95` computes the conflict target as `f"{stem}.{language}.srt"`.
  It will use the candidate's format when known and otherwise test every
  supported extension for `{stem}.{language}.*`, so an existing `.ass` is not
  silently overwritten.
- `library/SubDL.py:304` allows only `("srt", "ass", "vtt")` and downgrades
  anything else to SRT. `"ssa"` is added.
- `library/SubDL.py:359-364` scans extracted archives for `.ass` and `.srt`
  only. `.ssa` is added to both the scan and the selection.
- `library/OpenSubtitles.py:249` hardcodes `.srt` in `process_media_file`. This
  is the legacy CLI path rather than the TUI, but it is fixed for consistency so
  the two entry points do not disagree.

## English fallback

Two new `general:` settings:

- `fallback_language: str = "en"` — the language searched when the target
  language yields nothing. An empty value disables fallback searching entirely.
- `auto_fallback_download: bool = False` — opt-in automatic download of the best
  fallback candidate. Default `false`.

Behaviour, matching the approved option (c) with a default of (a):

1. Search the target language first.
2. If the filtered and deduplicated result set is empty, and `fallback_language`
   is non-empty and differs from the requested language, search the fallback
   language.
3. Arabic results are never replaced by English results. When the fallback runs,
   the result is marked as a fallback and the fallback candidates are presented
   for manual choice. Nothing is downloaded automatically unless
   `auto_fallback_download` is `true`.
4. With `auto_fallback_download: true`, the single best fallback candidate is
   downloaded and post-processed exactly as a primary candidate would be.

"Acceptable candidates" means exactly: the candidate list surviving
`_is_visible` filtering and deduplication in `SearchCoordinator._prepare` is
empty. No score threshold is introduced.

Orchestration lives in one new method on `SearchCoordinator` to guarantee TUI and
headless parity:

```python
def search_with_fallback(self, request, run_search) -> CoordinatedSearchResult
```

`run_search` is the caller's existing mode entry point (`all_providers` or
`auto`), passed in rather than re-derived, so no mode dispatch is duplicated.
`CoordinatedSearchResult` gains `used_fallback: bool = False` and
`fallback_language: str | None = None`.

Both `SubsApp.run_search` (`tui/app.py:695`) and
`HeadlessAllProvidersRunner.run` (`tui/headless.py:46-133`) call this method, so
the behaviour cannot diverge between interfaces.

### Headless behaviour

No human is present to choose. With the default
`auto_fallback_download: false`, a headless run that only finds fallback
candidates reports them using the existing `Notice:` output convention and exits
without downloading, stating that `general.auto_fallback_download` enables
automatic fallback download. It never silently downloads a language the user did
not ask for.

## Configuration surface

New fields under `general:`:

| Field | Default | Purpose |
|---|---|---|
| `fallback_language` | `"en"` | Language searched when the target language yields nothing. Empty disables. |
| `auto_fallback_download` | `false` | Opt in to automatic fallback download. |

Changed defaults:

| Field | Was | Now | Reference |
|---|---|---|---|
| `default_language` | `""` | `"ar"` | `tui/config.py:37` |
| `sync_audio_to_subs` | `"ask"` | `"false"` | `tui/config.py:41` |
| `preferred_backend` | `EngineMode.ASK` | `EngineMode.ALL_PROVIDERS` | `tui/config.py:36` |

`config.yaml.sample` mirrors all five settings, and `sync_audio_to_subs` keeps
its documented `true`/`false`/`ask` options with `false` now the default.

**Each changed default requires two edits, not one.** `ConfigRepository.load()`
repeats every default inline in its `.get()` fallback rather than relying on the
dataclass default, so changing only the dataclass has no effect on any loaded
configuration — including a configuration file that omits the key entirely:

| Field | Dataclass | Inline fallback in `load()` |
|---|---|---|
| `default_language` | `tui/config.py:37` | `tui/config.py:164` — `general_raw.get("default_language", "")` |
| `sync_audio_to_subs` | `tui/config.py:41` | `tui/config.py:172` — `general_raw.get("sync_audio_to_subs", "ask")` |
| `preferred_backend` | `tui/config.py:36` | `tui/config.py:159` — `general_raw.get("preferred_backend", EngineMode.ASK.value)` |

This is the same class of silent failure as `SUPPORTED_GENERAL_FIELDS` below,
and a test asserting the new defaults must load a config that omits all three
keys, so that a missed inline fallback fails the suite.

Adding fields under the existing `general:` section requires updating four
places. Missing any of them produces a silent failure:

1. `SUPPORTED_GENERAL_FIELDS` (`tui/config.py:18-30`) — a field absent from this
   set is never written back by `_prepare()`.
2. `ConfigRepository.load()` (`tui/config.py:146-216`).
3. `ConfigRepository._prepare()` (`tui/config.py:231-294`).
4. `SubsApp._overlay_raw_config()` (`tui/app.py:590-643`).

The `known` set at `tui/config.py:212` lists section names and does not change,
because both new fields live under the existing `general:` section.

## User interface

Minimal changes only. No screen is redesigned.

- `tui/widgets/results_table.py`: add a `Format` column beside the existing
  `Source` and `Match` columns. Current widths are `#`=4 (`:100`), `Release`=62
  or 71 depending on `all_providers_mode` (`:101`), `L`=2 (`:102`), `Source`=9
  (`:104`), `Flags`=6 (`:105`), `D/L`=5 (`:106`), `Match`=5 (`:107`). The
  `Release` and `Source` widths are rebalanced to absorb the new column while
  keeping the total within the wide-layout budget set by
  `WIDE_LAYOUT_MIN_WIDTH = 140` (`tui/app.py:62`).
- `tui/widgets/detail_pane.py`: show the candidate's `format`, and its match
  reasons.
- Fallback presentation uses the existing notification channel
  (`tui/app.py:1304`) plus the existing `L` column, which already displays the
  language code and therefore shows `EN` for fallback candidates.
- Provider and release name are already displayed by the existing `Source` and
  `Release` columns.

## Error handling

- Arabic folding is total and never raises on `None`, empty, or non-Arabic
  input.
- A failure in the fallback search does not discard the primary result. The
  primary (empty) result stands and the fallback error is recorded through the
  existing `CoordinatedSearchResult.errors` channel.
- If `fallback_language` is empty or equals the requested language, no fallback
  search runs.
- Format detection never corrupts a file: it renames only within the staging
  `TemporaryDirectory` before `os.replace`, and an ambiguous result falls back
  to `.srt`.
- Existing secret redaction (`tui/providers/base.py:49-60`) continues to apply
  to every provider error string.

## Test strategy

Extend the existing focused test files. Follow the established style: plain
pytest, `asyncio.run(inner())` wrappers instead of `pytest-asyncio`, network
calls monkeypatched on the real `library.*` classes, and no new shared fixtures
in `tui/tests/conftest.py`.

`tui/tests/test_scoring.py`:

- Each folding rule individually: tashkeel, tatweel, alef forms, teh marbuta,
  alef maksura, Arabic-Indic digits, Extended Arabic-Indic digits.
- Folding is idempotent and leaves Latin-only strings byte-identical.
- An Arabic title with and without diacritics scores as the same title.
- `score_subtitle` returns the same numeric value as before for every existing
  fixture.
- `explain_subtitle_match` returns at most six reasons and includes a year
  reason and an episode reason for a matching release.
- The existing negative cases still hold: a bare episode adds only a bounded
  nudge, a year or resolution is not treated as an episode number, and one
  shared generic title token does not establish the same series.

`tui/tests/test_providers.py`:

- `get_alternate_names` returns non-empty variants for a movie filename with no
  episode number (the regression that currently returns `None`).
- Arabic variants are additive: the original title is still present.
- Definite-article stripping applies to a long token and is skipped for `الله`
  and for short tokens.
- A stem containing both scripts produces one variant per script.
- `candidate_from_standardized` populates `format` from `sub_format`, from
  `format`, and from a URL extension, and yields `None` for an unknown value.
- SubSource routes through `get_alternate_names`.

`tui/tests/test_jobs.py`:

- Content sniffing maps an ASS body to `.ass`, an SSA body to `.ssa`, and an
  SRT body to `.srt`.
- A known `candidate.format` is honoured without sniffing.
- The conflict pre-check detects an existing `.ass` file and does not overwrite
  it when `overwrite` is false.
- An unknown or ambiguous body falls back to `.srt`.

`tui/tests/test_config.py`:

- `fallback_language` and `auto_fallback_download` survive a load/save round
  trip and appear in `SUPPORTED_GENERAL_FIELDS`.
- The new defaults are `ar`, `false`, and `all-providers` when loading a
  configuration file whose `general:` section omits all three keys, proving each
  inline `.get()` fallback in `load()` was updated and not only the dataclass
  default.
- A configuration file that explicitly sets `default_language: en`,
  `sync_audio_to_subs: ask`, and `preferred_backend: ask` still loads those
  values, proving the new defaults did not become overrides.
- Comment preservation and `extra`-section survival still hold.

`tui/tests/test_search.py`:

- An Arabic candidate outranks a non-Arabic candidate at equal score.
- Provider reliability orders equal-score candidates SubSource, then
  OpenSubtitles, then SubDL.
- `search_with_fallback` does not run a second search when the target language
  returns candidates.
- It does run when the target language returns none, and marks `used_fallback`.
- It does not run when `fallback_language` is empty or equals the target.
- A fallback search error leaves the primary empty result intact.
- A scorer without `explain_subtitle_match` yields empty reasons and still
  scores.

`tui/tests/test_headless.py`:

- With `auto_fallback_download: false`, a fallback-only result set downloads
  nothing and reports the opt-in setting.
- With `auto_fallback_download: true`, the best fallback candidate downloads.

The full TUI test suite remains the regression gate after the focused tests
pass.

## Known limitations

- **Alternate titles cannot be discovered, only varied.** With no metadata
  source, a file named in Latin script can never reveal that its Arabic release
  title is different. Filename-derived variants do not close this gap; a title
  indexed by providers under a wholly different Arabic name will not be found.
  This is a capability limit, not a defect, and the `get_alternate_names` seam
  exists so Phase 2 can address it without touching providers.
- Provider reliability weights are static, hand-assigned values derived from
  `AUTO_PRIORITY`. They are not learned from download outcomes.
- Provider reliability is a tiebreak after score and hash match, not an additive
  score component, as described under Ranking.
- ASS/SSA preservation depends on a provider actually delivering those formats.
  Nothing in this design can recover a format a provider does not offer.
- The legacy `download_subs.py` menu flow shares the `library/` changes but its
  own prompts and wording are unchanged.
- Terminal rendering of Arabic subtitle text remains the terminal's
  responsibility. This design handles filenames and matching only.
- The duplicated provider order in `engine_switcher.py:43-46` (`HELP_TEXT`) and
  `AUTO_PRIORITY` is left as-is; only the ranking path is changed to derive from
  a single source.

## Divergence from upstream

The three default changes mean this fork behaves differently from
`ach-raf/opensubtitles_subtitle_downloader` for users who never configure a
language. That is the intent of an Arabic-first edition, but it is a real
divergence and should be stated in the README. Opting out requires no code
change: `--lang en`, `general.default_language: en`, and
`preferred_backend: ask` each restore the previous behaviour individually.

Local `master` is currently level with `upstream/master`, and all work happens
on the existing `feat/arabic-edition` branch, which has no commits yet.
