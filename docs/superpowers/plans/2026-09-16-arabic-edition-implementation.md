# Arabic Edition Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the application excellent at finding and downloading existing Arabic
subtitles — Arabic as the default target language, Arabic-aware matching and query
variants, best-match ranking with visible reasons, ASS/SSA/VTT format preservation,
and an explicit opt-in English fallback.

**Architecture:** Phase 1 threads Arabic through the layers that already exist rather
than adding new ones. `library/subtitle_utils.py` gains Arabic text folding and a
`MatchExplanation` return type; `get_alternate_names` becomes the single choke point
for query variants; `tui/domain.py` gains two `Candidate` fields; `tui/search.py`
composes ranking and reasons; `tui/jobs.py` and the provider adapters preserve
subtitle formats; `tui/config.py` carries five new/changed defaults; and one new
`SearchCoordinator.search_with_fallback` method is the only new orchestration.

**Tech Stack:** Python 3.10+, Textual 8.x TUI, `ruamel.yaml` round-trip config,
`thefuzz` fuzzy matching, pytest (plain functions, `asyncio.run` wrappers — no
`pytest-asyncio`).

**Spec:** `docs/superpowers/specs/2026-09-16-arabic-edition-design.md`

## Global Constraints

- Python floor is 3.10 (`pyproject.toml:6`). No `match`-less syntax gaps, no
  `X | Y` runtime unions outside `from __future__ import annotations` files.
- Line length 88; `ruff` rules `E, F, I, UP, B, SIM` (`pyproject.toml:37-42`).
- Tests are plain pytest functions. Async code is driven with
  `asyncio.run(inner())` wrappers. **No `pytest-asyncio` markers.**
- `tui/tests/conftest.py` gains **no** new fixtures. It only puts the repo root on
  `sys.path` (14 lines).
- **`tui/` must never import `library/` at module scope.** `library/subtitle_utils.py`
  imports `thefuzz` at module scope; a top-level import from `tui/` would make the
  TUI's test suite depend on a fuzzy-matching library for modules that do not use it.
  `tui/search.py` already respects this with a lazy import inside `SearchCoordinator.__init__`.
  Task 7 preserves that discipline.
- No AI translation, no UI localization, no RTL work, no new providers, no new
  metadata source, no screen redesign.
- Every commit message ends with the trailer
  `Co-Authored-By: Claude Code <noreply@anthropic.com>`.

---

## ⚠️ Required amendments to the approved spec

Self-review of the plan against the spec surfaced three defects in the spec. Each is
fixed **inside this plan** rather than worked around. They need the user's explicit
approval, because two of them change spec text and one changes spec intent.

### Amendment 1 — Arabic folding is a no-op without widening the character class (blocking)

`library/subtitle_utils.py:341` ends `_normalize_match_text` with:

```python
text = re.sub(r"[^0-9A-Za-z]+", " ", text)
```

This deletes **every** Arabic character. The spec's Arabic folding section
(`spec:65-94`) adds folding *before* this line, so folding would be undone one line
later. Measured against the real function:

```
_normalize_match_text('الهيبة.S01E03.1080p.WEB-DL')            -> 's01e03 1080p web dl'
_normalize_match_text('The.Pitt.S01E01.1080p.WEB-DL')          -> 'the pitt s01e01 1080p web dl'
_title_hypotheses('الهيبة.S01E03.1080p.WEB-DL')                -> []
_title_hypotheses('مسلسل.آخر.تماما.S01E03.1080p.WEB-DL')       -> []
_title_hypotheses('The.Pitt.S01E03.1080p')                     -> ['the pitt']
```

Consequences today, all of which Phase 1 exists to fix:

1. Every Arabic release yields zero title tokens, so `_title_match_score` returns
   `0.0` and `title_plausible` is `False`.
2. `score_subtitle` then applies `score = min(score, 15.0)` (`:753`). **Every Arabic
   candidate is capped at 15/100** while Latin candidates reach ~100.
3. Two *different* Arabic series both yield `[]`, so Arabic candidates are
   indistinguishable from each other and their relative order is arbitrary.

**Fix (Task 1):** widen the class to retain the Arabic blocks. This is not a new
feature — it is the precondition that makes the spec's own folding section
functional. Without it, Task 1's folding and every downstream Arabic behavior is
dead code.

### Amendment 2 — the reason table contradicts its own stated intent

`spec:200-202` says reasons are "truncated to the first six, so the least significant
evidence is dropped first and **the episode determination and provider are never
hidden**." But the table places `provider` at **#8** of 10. Truncation to six drops it.

The set of reasons that can fire simultaneously is bounded: #1 hash, one of #2/#3
(mutually exclusive), #4 season, #5/#6 year (mutually exclusive), #7 release name, #8
provider, #9 source, #10 resolution — **7 reasons maximum**, so truncation is reachable.

**Fix (Tasks 2 and 7):** keep the closed reason set and the cap of six, and reorder so
the two the spec names as never-hidden fall inside it. Concrete emitted order:

| # | Reason | Emitted when |
|---|---|---|
| 1 | `exact hash match` | `hash_match` is true |
| 2 | `movie/episode match` | Agree on the episode, or both carry none |
| 2 | `episode mismatch` | Both name an episode and they differ |
| 3 | `season mismatch` | Both name a season and they differ |
| 4 | `provider: {label} (reliability {n}/3)` | Always (inserted by `_prepare`) |
| 5 | `year match {year}` / `year mismatch ({year})` | Both name a year |
| 6 | `release name match` | The fuzzy title path met an existing threshold |
| 7 | `source: {SOURCE}` | A release source token is detected |
| 8 | `resolution: {RES}` | A resolution token is detected |

Worst case produces 7 reasons: `explain_subtitle_match` returns its six in order
(dropping `resolution`), then `_prepare` inserts the provider reason at index 2 and
truncates to six (dropping `source`). Only the two least significant reasons are ever
lost, and the episode determination and provider always survive.

Reason `provider` cannot come from `explain_subtitle_match`: the spec also requires
that function stay provider-agnostic (`spec:174-180`, and the approved decision to keep
provider reliability a tiebreak that never enters the score). `explain_subtitle_match`
therefore emits reasons 1–3 and 5–8, and `SearchCoordinator._prepare` — which knows
`candidate.provider` — inserts reason 4. No score is affected.

### Amendment 3 — `sync_audio_to_subs` default is stated in the wrong representation

`spec:331` lists the dataclass change as `"ask"` → `"false"`. But the dataclass field
holds the **normalized** token, not the on-disk token: `load()` runs the raw value
through `normalize_sync_policy`, which maps `False`/`"false"` → `"never"`. The config
tab `Select` (`tui/widgets/views.py:196-204`) has option values `"ask"`, `"always"`,
`"never"` — setting the field to the literal `"false"` would be an invalid `Select`
value, and `_prepare`'s `{"always": True, "never": False}.get("false", "ask")`
(`tui/config.py:244`) would silently write `ask` back.

**Fix (Task 8):** the dataclass default becomes `"never"`, the inline `load()` fallback
becomes `False`, and the resulting YAML value is `false`. Same behavior the spec
describes; correct representation.

### Amendment 4 — the results table has ~3 columns of slack, not room for a column

`spec:364-381` asks the results UI to show the format. The table's column widths are a
hard budget, not a preference, and the budget is nearly spent:

- `#results-panel` is `width: 18fr` (`tui/style.tcss:495`) against `DetailPane`'s
  `width: 7fr` (`tui/style.tcss:539`) — 25fr total.
- At `WIDE_LAYOUT_MIN_WIDTH = 140` (`tui/app.py:62`), that is roughly 100 columns for
  the panel, minus its 2-column border and `ResultsTable`'s `padding: 0 1`
  (`tui/style.tcss:511`) — **about 96 usable columns**.
- The current columns total **93** (`4 + 71 + 2 + 6 + 5 + 5`), so there are about
  **3 columns of slack**. A naive 6-wide `Format` column would overflow and make
  `max_scroll_x` non-zero, which two existing tests assert against
  (`tui/tests/test_app.py:869`, `:1113`).

Landing a format column therefore requires paying for it. Task 11 takes 1 column from
`#` (4→3), 1 from `Flags` (6→5), and 1 from `Match` (5→4), and spends 4 on a `Fmt`
column — total 94 in both modes, leaving ~2 columns of headroom.

Two of those shrinkages are at their floor and **must not be reduced further**:
`D/L` must stay 5 because `_count` renders `"48.2k"` (`tui/widgets/results_table.py:111-112`
and the fixture at `tui/tests/test_app.py:849`), and `Match` must stay 4 because
`_score` renders `" 100"` (`:115-116`, asserted at `tui/tests/test_app.py:868`).
`Release` must stay ≥ 71 in non-all-providers mode (`tui/tests/test_app.py:862`).

This arithmetic is derived from the stylesheet, not measured — `textual` is not
installed, so `test_app.py` cannot run until the Prerequisite is satisfied. Task 11's
verification step is therefore the authority, and it carries an explicit fallback
ladder.

### Pre-existing bug found in a file this plan edits

⚠️ **`tui/widgets/results_table.py:64-65` misaligns two columns in all-providers mode.**
`_set_columns` declares them `# Release L Source Flags D/L Match`, but the row builder
builds six cells and then does `cells.insert(2, f" {candidate.provider.label}")`,
producing `# Release Source L Flags D/L Match`. The result is that the 2-wide `L`
column renders the **provider name** (truncated to `"Su"` / `"Op"`) and the 9-wide
`Source` column renders the **language code** (`"AR"`).

The insert index should be 3. No test covers it: the only cell assertions are in
non-all-providers mode (`tui/tests/test_app.py:863`) and on column 0
(`:902-903`), and `test_all_providers_mode_dispatches_all_providers_search` (`:200`)
checks dispatch, not cells. Task 11 fixes this because it rewrites this exact
expression, and adding a `Fmt` column to a table with two swapped columns would make
the misalignment permanent. This is a one-line correction to a function the task
already changes — not an unrelated refactor.

### Deliberate non-change (recorded so a reviewer does not flag it)

`RunPolicy.audio_sync` (`tui/state.py:134`) keeps its `"ask"` default. `RunPolicy` is a
legacy compatibility object — `tui/state.py:1-8` says so — and **nothing constructs it
from config**: the only construction is `AppState.run_policy`'s own `default_factory`
(`tui/state.py:243`). Real sync behavior flows through
`config.general.sync_audio_to_subs`, consumed at `tui/headless.py:61`,
`tui/app.py:1092`, and `tui/providers/factory.py:34`. Changing `RunPolicy` would be an
unrelated refactor with no runtime effect.

---

## Prerequisite: the test suite cannot currently run

**Not a code change.** The environment is missing three declared dependencies, so
*all 12* TUI test modules fail at import and no red-green cycle can be observed.

```
$ python -m pytest tui/tests -q
ERROR tui/tests/test_scoring.py   ... ImportError: No module named 'thefuzz'
ERROR tui/tests/test_search.py    ... ModuleNotFoundError: No module named 'ruamel'
Interrupted: 12 errors during collection
```

- `thefuzz`, `ruamel.yaml`, `textual` are all absent.
- `thefuzz` and `ruamel.yaml` are declared in both `requirements.txt` and
  `pyproject.toml:8-20`; `textual>=8,<9` likewise. This is an unprovisioned
  environment, not a repo defect.
- `uv` is not installed and there is no `.venv`, though the project declares a uv
  workflow (`pyproject.toml:29-31`, commit `ca0e7da`).

**Blocking action before Task 1** — pick one:

```bash
uv sync                      # project's declared workflow; needs uv installed
# or
python -m pip install -r requirements.txt
```

**Verification that the prerequisite is met:**

```bash
python -m pytest tui/tests -q
```

Expected: collection succeeds. Record the pass/fail counts as the baseline before
Task 1; the spec's `spec:472` makes the full suite the regression gate, and Tasks 1,
2, and 8 deliberately change numbers that existing tests may assert. **Any test that
fails on this baseline is pre-existing drift — do not fix it in this plan, and do not
attribute it to these changes.**

---

## File Structure

| File | Responsibility in this plan | Tasks |
|---|---|---|
| `library/subtitle_utils.py` | Arabic folding, normalization widening, `MatchExplanation`, reason assembly, query variants | 1, 2, 3 |
| `tui/domain.py` | `SUBTITLE_FORMATS` + `normalize_subtitle_format`; `Candidate.format`, `Candidate.match_reasons` | 4 |
| `tui/providers/base.py` | Populate `Candidate.format` at the provider boundary | 4 |
| `tui/providers/opensubtitles.py` | Stop forcing `.srt` on the staging path | 4 |
| `library/SubDL.py`, `library/OpenSubtitles.py` | Legacy CLI format preservation | 5 |
| `tui/jobs.py` | Content sniffing; format-aware conflict pre-check | 6 |
| `tui/search.py` | Language priority, provider reliability, reason composition | 7, 9 |
| `tui/config.py` | Two new fields, three changed defaults, four edit sites | 8 |
| `config.yaml.sample` | Mirror the five settings | 8 |
| `tui/app.py` | Fallback wiring; format column budget | 10, 11 |
| `tui/headless.py` | Fallback wiring with no-human semantics | 10 |
| `tui/widgets/results_table.py`, `tui/widgets/detail_pane.py` | `Format` column; reasons display | 11 |
| `Readme.md` | Divergence note | 12 |

Test files are the six the spec names (`spec:400-473`), extended in place. No new test
file and no new fixture.

---

## Task 1: Arabic-preserving text normalization

**Why first:** every other Arabic task depends on Arabic characters surviving
normalization (Amendment 1). Without this task the rest of Phase 1 is inert.

**Files:**
- Modify: `library/subtitle_utils.py` (module constants after `APOSTROPHE_RE` at `:26`; `_normalize_match_text` at `:335-342`)
- Test: `tui/tests/test_scoring.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `fold_arabic(value) -> str` (module-level, total); `ARABIC_MARKS_RE`,
  `TATWEEL_RE`, `ALEF_FORMS_RE`, `ARABIC_DIGITS_RE`, `KEEP_MATCH_CHARS_RE`. Task 3
  calls `fold_arabic`; Tasks 2 and 7 rely on `_normalize_match_text` now retaining
  Arabic.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_scoring.py`. Change the import line at the top to:

```python
from library.subtitle_utils import SubtitleUtils, fold_arabic
```

Add:

```python
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
    assert scorer._normalize_match_text("الهيبة.S01E03.1080p.WEB-DL") == (
        "الهيبة s01e03 1080p web dl"
    )


def test_latin_text_normalization_is_unchanged(scorer):
    assert scorer._normalize_match_text("The.Pitt.S01E01.1080p.WEB-DL") == (
        "the pitt s01e01 1080p web dl"
    )


def test_arabic_title_yields_a_hypothesis(scorer):
    # The precondition for Arabic matching: hypotheses must be non-empty and must
    # carry the Arabic title through. Asserted as a property rather than an exact
    # list, because the hypothesis text is an internal formatting detail.
    hypotheses = scorer._title_hypotheses("الهيبة.S01E03.1080p.WEB-DL")

    assert hypotheses
    assert any("الهيبة" in hypothesis for hypothesis in hypotheses)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_scoring.py -q -k "fold_arabic or arabic or latin_text"`
Expected: FAIL — `ImportError: cannot import name 'fold_arabic'`.

- [ ] **Step 3: Write the minimal implementation**

In `library/subtitle_utils.py`, insert immediately after the `APOSTROPHE_RE`
definition (after line 26):

```python
# Arabic orthographic folding for matching. Providers and release groups spell the
# same Arabic title many ways: with or without harakat, with any of the alef forms,
# with Arabic-Indic digits. Folding them to one canonical spelling is what lets two
# spellings of one title compare equal.
# Matching side only -- normalize_media_name() builds provider *queries*, and folding
# a query would remove matches rather than add them.
ARABIC_MARKS_RE = re.compile(r"[ً-ٰٟۖ-ۭ]")
TATWEEL_RE = re.compile(r"ـ")
ALEF_FORMS_RE = re.compile(r"[آأإٱ]")
ARABIC_DIGITS_RE = re.compile(r"[٠-٩۰-۹]")

# The characters _normalize_match_text keeps. Everything outside this class becomes a
# separator, so the Arabic ranges must be listed explicitly -- otherwise the folding
# above is undone by the very next line.
KEEP_MATCH_CHARS_RE = re.compile(
    r"[^0-9A-Za-z"
    r"؀-ۿ"  # Arabic
    r"ݐ-ݿ"  # Arabic Supplement
    r"ࢠ-ࣿ"  # Arabic Extended-A
    r"ﭐ-﷿"  # Arabic Presentation Forms-A
    r"ﹰ-﻿"  # Arabic Presentation Forms-B
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
    text = ALEF_FORMS_RE.sub("ا", text)
    text = text.replace("ة", "ه")
    text = text.replace("ى", "ي")
    return ARABIC_DIGITS_RE.sub(_fold_arabic_digit, text)
```

Replace `_normalize_match_text` (lines 335-342) with:

```python
    @staticmethod
    def _normalize_match_text(value):
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"[‐-―−]", "-", text)
        text = APOSTROPHE_RE.sub("", text)
        text = fold_arabic(text)
        text = text.replace("_", " ").replace(".", " ")
        text = KEEP_MATCH_CHARS_RE.sub(" ", text)
        return " ".join(text.lower().split())
```

Note: Arabic is caseless, so `.lower()` is a no-op on it. `U+0624` and `U+0626` are
separate letters and are deliberately not folded. Arabic presentation forms are
already mapped to their base letters by the existing NFKC step and need no extra
handling.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_scoring.py -q`
Expected: PASS — all new tests plus the pre-existing assertions in the file.

The pre-existing tests must be unaffected: every existing fixture is Latin-only, and
for Latin input the widened class behaves identically to `[^0-9A-Za-z]`.

- [ ] **Step 5: Commit**

```bash
git add library/subtitle_utils.py tui/tests/test_scoring.py
git commit -m "feat: fold Arabic orthographic variants and stop erasing Arabic in match normalization

The 'drop everything else' pass in _normalize_match_text deleted every Arabic
character, so every Arabic release produced zero title tokens, scored 0.0 for
title plausibility, and was capped at 15/100. Widen the retained character
class to the Arabic blocks so the new folding is not undone one line later.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 2: `MatchExplanation` and `explain_subtitle_match`

**Files:**
- Modify: `library/subtitle_utils.py` (`_title_match_score` at `:478-517`, `score_subtitle` at `:693-758`)
- Test: `tui/tests/test_scoring.py`

**Interfaces:**
- Consumes: `_normalize_match_text` (Task 1), `_episode_evidence`, `_title_hypotheses`.
- Produces: `MAX_MATCH_REASONS = 6`; `MatchExplanation(score: float, reasons: tuple[str, ...])`;
  `SubtitleUtils.explain_subtitle_match(release, query, hash_match=False) -> MatchExplanation`.
  Task 7 calls `explain_subtitle_match` and slices to `MAX_MATCH_REASONS`.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_scoring.py`:

```python
# --- Match explanations ---------------------------------------------------

def test_score_subtitle_is_unchanged_by_the_explanation_refactor(scorer):
    # Every existing fixture, asserted against the value the pre-refactor
    # implementation produced.
    assert scorer.score_subtitle("The.Pitt.S01E01.1080p.WEB.H264-SuccessfulCrab", PITT_QUERY) == (
        scorer.explain_subtitle_match(
            "The.Pitt.S01E01.1080p.WEB.H264-SuccessfulCrab", PITT_QUERY
        ).score
    )
    assert scorer.score_subtitle(UNRELATED_S01E01, PITT_QUERY) == (
        scorer.explain_subtitle_match(UNRELATED_S01E01, PITT_QUERY).score
    )


def test_explanation_score_is_a_float_for_all_fixtures(scorer):
    for release in (
        "The.Pitt.S01E01.1080p.WEB.H264-SuccessfulCrab",
        "The.Pitt.S02E01.1080p.WEB.h264-ETHEL.ar",
        UNRELATED_S01E01,
        "The.Pitt.Arabic.WEB-DL",
    ):
        explanation = scorer.explain_subtitle_match(release, PITT_QUERY)
        assert isinstance(explanation.score, float)
        assert isinstance(explanation.reasons, tuple)


def test_hash_match_scores_100_and_reports_only_the_hash(scorer):
    explanation = scorer.explain_subtitle_match(
        "Anything.At.All", PITT_QUERY, hash_match=True
    )

    assert explanation.score == 100.0
    assert explanation.reasons == ("exact hash match",)


def test_explanation_reports_episode_and_year_for_a_matching_release(scorer):
    explanation = scorer.explain_subtitle_match(UNRELATED_S01E01, PITT_QUERY)

    assert "episode mismatch" in explanation.reasons


def test_explanation_reports_year_match_for_agreeing_years(scorer):
    explanation = scorer.explain_subtitle_match(
        "Widows.Bay.2026.S01E01.ATVP.WEB-DL.2160p.HDR.H.265",
        WIDOWS_BAY_QUERY,
    )

    assert "year match 2026" in explanation.reasons


def test_explanation_reports_year_mismatch_for_differing_years(scorer):
    explanation = scorer.explain_subtitle_match(
        "Widows.Bay.2019.S01E01.1080p.WEB-DL",
        WIDOWS_BAY_QUERY,
    )

    assert "year mismatch (2019)" in explanation.reasons


def test_explanation_reports_release_name_match_for_a_fuzzy_title(scorer):
    explanation = scorer.explain_subtitle_match(
        "The.Pitt.S01E01.1080p.WEB.H264-SuccessfulCrab",
        PITT_QUERY,
    )

    assert "release name match" in explanation.reasons


def test_explanation_reports_source_and_resolution(scorer):
    explanation = scorer.explain_subtitle_match(
        "The.Pitt.S01E01.1080p.WEB-DL.x264",
        PITT_QUERY,
    )

    assert "source: WEB-DL" in explanation.reasons
    assert "resolution: 1080p" in explanation.reasons


def test_explanation_never_exceeds_the_reason_cap(scorer):
    explanation = scorer.explain_subtitle_match(
        "The.Pitt.S01E01.1080p.WEB-DL.x264",
        PITT_QUERY,
    )

    assert len(explanation.reasons) <= MAX_MATCH_REASONS


def test_explanation_is_total_for_empty_input(scorer):
    assert scorer.explain_subtitle_match("", PITT_QUERY).score == 0.0
    assert scorer.explain_subtitle_match("x", "").score == 0.0
```

Also extend the import line:

```python
from library.subtitle_utils import MAX_MATCH_REASONS, SubtitleUtils, fold_arabic
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_scoring.py -q -k explanation`
Expected: FAIL — `ImportError: cannot import name 'MAX_MATCH_REASONS'`.

- [ ] **Step 3: Write the minimal implementation**

In `library/subtitle_utils.py`, add after the `fold_arabic` function:

```python
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
```

Add `from dataclasses import dataclass` to the imports at the top of the file
(after `import unicodedata`).

Replace `_title_match_score` (lines 478-517) with a version returning `_TitleMatch`.
The arithmetic is **unchanged**; only the return type and the `fuzzy_matched` flag are
new. It has exactly one caller (`:711`).

```python
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
```

Add these two helpers immediately before `score_subtitle`:

```python
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
```

Replace `score_subtitle` (lines 693-758) with `explain_subtitle_match` plus a thin
wrapper. The scoring arithmetic is copied **verbatim** from the current body; only the
reason collection is added. `score_subtitle` keeps its exact signature and return
type, and `except` still returns `0` (not `0.0`) so no existing consumer sees a
changed type on the error path.

```python
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
```

Note the two deliberate details:

- The `episode mismatch` reason is computed from evidence (`both name an episode and
  they differ`) as the spec's table specifies, which is **broader** than the `-15.0`
  branch above it. The reason reports the evidence; the penalty has its own stricter
  guard. They are intentionally not the same condition.
- `hash_match` returns early with exactly one reason, preserving the original
  `return 100.0` and keeping the reason list trivially within the cap.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_scoring.py -q`
Expected: PASS, including every pre-existing assertion in the file — the arithmetic
was copied verbatim and no fixture's inputs changed.

- [ ] **Step 5: Commit**

```bash
git add library/subtitle_utils.py tui/tests/test_scoring.py
git commit -m "refactor: return match explanations from subtitle scoring

score_subtitle becomes a thin wrapper over explain_subtitle_match so the number
and its stated reasons can never disagree. Numeric behavior is unchanged; the
reason set is closed at six and ordered most significant first.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 3: Arabic query variants and the movie gap

**Files:**
- Modify: `library/subtitle_utils.py` (`get_alternate_names` at `:544-659`)
- Modify: `library/SubSource.py` (`_gather_candidates` at `:357-398`)
- Test: `tui/tests/test_providers.py`

**Interfaces:**
- Consumes: `fold_arabic` (Task 1).
- Produces: `get_alternate_names(media_name) -> list[str] | None`, now non-`None` for
  movies and additive with Arabic variants; `SubtitleUtils._title_variants(title) -> list[str]`.

`get_alternate_names` has exactly two callers today (`library/OpenSubtitles.py:142`,
`library/SubDL.py:220`) and must keep returning a de-duplicated `list[str]`. Consumers
do `get_alternate_names(x) or []`, so an empty list and `None` are both safe.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_providers.py`:

```python
# --- Alternate-name query variants ----------------------------------------

MOVIE_FILENAME = "The.Pitt.2025.1080p.WEB-DL.x264-FLUX"


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

    assert any(name.strip() == "The.Pitt" or name.strip() == "The Pitt" for name in names)


def test_get_alternate_names_still_handles_episodes():
    names = SubtitleUtils().get_alternate_names("The.Pitt.S01E01.1080p.WEB-DL")

    assert names
    assert any("S01E01" in name for name in names)


def test_arabic_variant_is_additive_and_folded():
    names = SubtitleUtils().get_alternate_names("الهيبة.S01E03.1080p.WEB-DL")

    assert names
    assert any("الهيبة" in name for name in names)


def test_definite_article_is_stripped_from_a_long_token():
    names = SubtitleUtils().get_alternate_names("المشروع.S01E01.1080p")

    assert any("مشروع" in name for name in names)


def test_definite_article_is_not_stripped_from_short_tokens():
    names = SubtitleUtils().get_alternate_names("البيت.S01E01.1080p")

    # "بيت" is only 3 characters, so the stripped form is not generated.
    assert not any("بيت" in name and "البيت" not in name for name in names)


def test_definite_article_is_never_stripped_from_allah():
    names = SubtitleUtils().get_alternate_names("الله.S01E01.1080p")

    assert not any("لله" in name and "الله" not in name for name in names)


def test_mixed_script_stem_produces_one_variant_per_script():
    names = SubtitleUtils().get_alternate_names("Al-Hayba.الهيبة.S01E03.1080p")

    assert any("الهيبة" in name for name in names)
    assert any("Al-Hayba" in name or "Al Hayba" in name for name in names)
```

Confirm `SubtitleUtils` is imported in that file; if not, add
`from library.subtitle_utils import SubtitleUtils` alongside the existing imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_providers.py -q -k alternate_names or arabic_variant or definite_article or mixed_script`
Expected: FAIL — `test_get_alternate_names_generates_variants_for_a_movie` gets `None`.

- [ ] **Step 3: Write the minimal implementation**

In `library/subtitle_utils.py`, add this helper immediately before
`get_alternate_names`:

```python
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
```

Then rewrite `get_alternate_names` so the format-generation block runs **once per
title variant** instead of once per title. Replace the body from the `if not episode`
guard through the `formats` generation:

```python
    def get_alternate_names(self, media_name):
        """Generate alternate name formats for the media"""
        try:
            if not media_name:
                return None

            # First get season/episode since we have robust parsing for that
            season, episode = self.extract_season_and_episode(media_name)

            # Extract title and year, now knowing where season/episode info is
            # Remove common episode/season patterns
            clean_name = media_name

            patterns_to_remove = [
                r"[Ss]\d{1,2}[Ee]\d{1,2}",
                r"[Ss]\d{1,2}\s*-\s*[Ee]\d{1,2}",
                r"\d{1,2}x\d{1,2}",
                r"(?:Episode|Ep)\s*\d{1,2}",
                r"[Ee]\d{1,2}",
                r"[Ee][Pp]\d{1,2}",
            ]

            for pattern in patterns_to_remove:
                clean_name = re.sub(pattern, "", clean_name, flags=re.IGNORECASE)

            # Extract year if present
            year_match = re.search(r"\((\d{4})\)", clean_name)
            year = year_match.group(1) if year_match else ""
            if not year:
                loose_year = re.search(r"\b((?:19|20)\d{2})\b", clean_name)
                year = loose_year.group(1) if loose_year else ""
            if year:
                clean_name = re.sub(r"\s*\(\d{4}\)\s*", " ", clean_name)
                clean_name = re.sub(rf"\b{year}\b", " ", clean_name)

            # Clean up title
            title = clean_name.strip().strip(".-_ ")
            if not title:
                return None

            formats = []

            # Movies have no episode number: title-only variants. Previously this
            # returned None, so films received no alternate queries at all.
            if not episode:
                for variant in self._title_variants(title):
                    formats.append(variant)
                    if year:
                        formats.append(f"{variant} {year}")
                        formats.append(f"{variant} ({year})")
                return list(dict.fromkeys(formats))

            for variant in self._title_variants(title):
                formats.extend(self._episode_formats(variant, year, season, episode))

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
```

**Removed:** the `Mr.`/`Ms.` post-processing block (original lines 632-652). It
re-scanned the *already generated* format strings for `Mr.`/`Ms.` and appended
`Mister`/`Miss` duplicates of every one. With `_title_variants` now generating the
title spellings up front, that block would only multiply near-duplicate queries against
live provider APIs. The `Mr.`/`Ms.` handling at the title level (original lines 583-597)
is folded into `_title_variants`' callers by keeping the original title first. If the
existing suite asserts on `Mister`/`Miss` output, stop and report rather than
re-adding the block — the assertion is drift this change is entitled to update.

Then wire SubSource. In `library/SubSource.py`, replace the `_resolve_movie_ids` call
in `_gather_candidates` (lines 366-367):

```python
        # Resolve candidate movieIds (one per season for TV). Fall back to the
        # alternate-name variants when the primary name resolves nothing, so
        # SubSource searches the same query space as the other two providers.
        pairs = self._resolve_movie_ids(media_name, video_season, video_episode)
        if not pairs:
            for term in self.subtitle_utils.get_alternate_names(media_name) or []:
                pairs = self._resolve_movie_ids(term, video_season, video_episode)
                if pairs:
                    break
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_providers.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add library/subtitle_utils.py library/SubSource.py tui/tests/test_providers.py
git commit -m "feat: generate Arabic and movie query variants in get_alternate_names

Movies no longer receive zero alternate names, and the format block now runs once
per title spelling so Arabic titles, article-stripped forms, folded forms, and
per-script splits are all searched. SubSource routes through the same generator.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 4: Format vocabulary and the provider boundary

**Files:**
- Modify: `tui/domain.py` (`Candidate` at `:57-91`)
- Modify: `tui/providers/base.py` (`candidate_from_standardized` at `:89-132`)
- Modify: `tui/providers/opensubtitles.py:16`
- Test: `tui/tests/test_providers.py`, `tui/tests/test_domain.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SUBTITLE_FORMATS = ("srt", "ass", "ssa", "vtt", "sub")` and
  `normalize_subtitle_format(value) -> str | None` in `tui/domain.py`;
  `Candidate.format: str | None = None`; `Candidate.match_reasons: tuple[str, ...] = ()`.
  Tasks 6, 7, and 11 consume all of these.

`normalize_subtitle_format` lives in `tui/domain.py`, not `library/`, so
`tui/providers/base.py` can import it without pulling `thefuzz` into the TUI import
graph (Global Constraints).

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_providers.py`:

```python
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


def test_normalize_subtitle_format_is_total():
    assert normalize_subtitle_format(None) is None
    assert normalize_subtitle_format("") is None
    assert normalize_subtitle_format("   ") is None
    assert normalize_subtitle_format("mkv") is None
```

Add to `tui/tests/test_domain.py`:

```python
def test_candidate_public_dict_includes_format_and_match_reasons():
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
        format="ass",
        match_reasons=("exact hash match",),
    )

    public = candidate.as_public_dict()

    assert public["format"] == "ass"
    assert public["match_reasons"] == ["exact hash match"]


def test_candidate_defaults_are_format_none_and_no_reasons():
    candidate = Candidate(
        provider=Provider.SUBDL,
        provider_id="1",
        release="R",
        language="ar",
    )

    assert candidate.format is None
    assert candidate.match_reasons == ()
```

Add the needed imports to each test file
(`from tui.domain import Candidate, Provider, normalize_subtitle_format` and
`from tui.providers.base import candidate_from_standardized`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_providers.py tui/tests/test_domain.py -q -k "format or public_dict"`
Expected: FAIL — `ImportError: cannot import name 'normalize_subtitle_format'`.

- [ ] **Step 3: Write the minimal implementation**

In `tui/domain.py`, add after the `EngineMode` class:

```python
# Subtitle container formats this application recognizes end to end. Anything else
# is treated as unknown so callers fall back to content sniffing rather than guess.
SUBTITLE_FORMATS = ("srt", "ass", "ssa", "vtt", "sub")


def normalize_subtitle_format(value) -> str | None:
    """Return a known subtitle format from a format string, URL, or file name.

    Total: anything unrecognized returns ``None`` rather than a default, so a caller
    can distinguish "known to be SRT" from "unknown, sniff the file".
    """
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in SUBTITLE_FORMATS:
        return text
    leaf = text.rsplit("/", 1)[-1].rsplit("?", 1)[0].rsplit("#", 1)[0]
    if "." not in leaf:
        return None
    extension = leaf.rsplit(".", 1)[-1]
    return extension if extension in SUBTITLE_FORMATS else None
```

Grow `Candidate` (`tui/domain.py:57-71`) with two fields, placed after
`ai_translated`:

```python
    format: str | None = None
    match_reasons: tuple[str, ...] = ()
```

Extend `as_public_dict` (`tui/domain.py:77-91`) before `"score"`:

```python
            "format": self.format,
            "match_reasons": list(self.match_reasons),
```

In `tui/providers/base.py`, import the helper and populate the field. Change the
import block to:

```python
from tui.domain import (
    Candidate,
    DownloadResult,
    HealthResult,
    Provider,
    ProviderSearchResult,
    SearchRequest,
    normalize_subtitle_format,
)
```

In `candidate_from_standardized`, add before the `return Candidate(`:

```python
    raw_format = (
        attributes.get("sub_format")
        or attributes.get("format")
        or attributes.get("file_name")
        or url
    )
```

and add to the `Candidate(...)` call, after `author=...`:

```python
        format=normalize_subtitle_format(raw_format),
```

In `tui/providers/opensubtitles.py`, replace line 16:

```python
        extension = normalize_subtitle_format(candidate.format) or "srt"
        target = media_path.with_name(
            f"{media_path.stem}.{candidate.language}.{extension}"
        )
```

and add `normalize_subtitle_format` to that module's `from tui.domain import (...)`
list.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_providers.py tui/tests/test_domain.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tui/domain.py tui/providers/base.py tui/providers/opensubtitles.py tui/tests/test_providers.py tui/tests/test_domain.py
git commit -m "feat: carry subtitle format on Candidate and stop forcing .srt

Provider metadata now populates Candidate.format, and the OpenSubtitles adapter
derives its staging extension from it instead of hardcoding .srt.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 5: Legacy CLI format preservation

**Files:**
- Modify: `library/SubDL.py:304`, `library/SubDL.py:359-364`
- Modify: `library/OpenSubtitles.py:249`
- Test: `tui/tests/test_providers.py`

**Interfaces:**
- Consumes: nothing new — this task only widens existing allow-lists so `ssa` survives.
- Produces: no new API.

These are the legacy `download_subs.py` paths, not the TUI, but the two entry points
must not disagree about which formats are acceptable.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_providers.py`:

```python
# --- Legacy CLI format preservation ---------------------------------------

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
        "/x/file.zip",
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
```

Add these small helpers to the same file if not already present:

```python
class _FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _SilentConsole:
    def print(self, *args, **kwargs):
        return None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_providers.py -q -k "ssa"`
Expected: FAIL — the `.ssa` format is downgraded to `.srt`, and `".ssa"` does not
appear in `_download_zip`.

- [ ] **Step 3: Write the minimal implementation**

In `library/SubDL.py`, line 304:

```python
        ext = f".{fmt}" if fmt in ("srt", "ass", "ssa", "vtt") else ".srt"
```

In `library/SubDL.py`, replace lines 359-364:

```python
                ass_files = [f for f in extracted_files if f.endswith(".ass")]
                ssa_files = [f for f in extracted_files if f.endswith(".ssa")]
                srt_files = [f for f in extracted_files if f.endswith(".srt")]

                if not ass_files and not ssa_files and not srt_files:
                    self.console.print(
                        "[bold red]Error: No .ass, .ssa, or .srt subtitle files "
                        "found in the archive.[/]"
                    )
                    return None
```

and replace the two uses of `ass_files + srt_files` below it with
`ass_files + ssa_files + srt_files`.

In `library/OpenSubtitles.py`, replace line 249:

```python
                f"{path.stem}.{language_choice}.srt",
```

with:

```python
                f"{path.stem}.{language_choice}.srt",
```

— **leave this line alone.** It builds the *conflict-detection* target for the legacy
path, and the legacy OpenSubtitles download (`:249`'s sibling at the save site) writes
SRT. Changing the check without changing the writer would make the two disagree.
Instead, add a comment above it recording the asymmetry:

```python
            # Legacy OpenSubtitles always writes SRT here; this check must match the
            # writer below, not the TUI's format-preserving path.
```

If inspection shows the legacy save site *does* preserve a provider-supplied
extension, change both together instead. Verify before editing; do not assume.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_providers.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add library/SubDL.py library/OpenSubtitles.py tui/tests/test_providers.py
git commit -m "feat: keep .ssa subtitles in the legacy SubDL download path

The allow-list downgraded anything but srt/ass/vtt to SRT, and the archive scan
ignored .ssa members entirely, so SSA packs were silently converted or missed.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 6: Content sniffing and a format-aware conflict check

**Files:**
- Modify: `tui/jobs.py` (`download` conflict check at `:95`, `_stage_download` at `:118-159`)
- Test: `tui/tests/test_jobs.py`

**Interfaces:**
- Consumes: `Candidate.format`, `SUBTITLE_FORMATS` (Task 4).
- Produces: `sniff_subtitle_format(path: Path) -> str` in `tui/jobs.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_jobs.py`:

```python
# --- Format sniffing ------------------------------------------------------

ASS_BODY = "[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\nFormat: Name\n"
SSA_BODY = "[Script Info]\nScriptType: v4.00\n[V4 Styles]\nFormat: Name\n"
SRT_BODY = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


@pytest.mark.parametrize(
    ("body", "expected"),
    [(ASS_BODY, "ass"), (SSA_BODY, "ssa"), (SRT_BODY, "srt"), ("", "srt")],
)
def test_sniff_subtitle_format_maps_content_to_extension(tmp_path, body, expected):
    path = tmp_path / "subtitle.bin"
    path.write_text(body, encoding="utf-8")

    assert sniff_subtitle_format(path) == expected


def test_an_ssa_body_wins_over_the_shared_script_info_marker(tmp_path):
    # SSA files also carry [Script Info]; the SSA-specific marker must be tested first.
    path = tmp_path / "subtitle.bin"
    path.write_text(SSA_BODY, encoding="utf-8")

    assert sniff_subtitle_format(path) == "ssa"
```

Then add the download-level tests. Match the existing fixture style in this file for
building a coordinator, adapter, and media path — read the file's existing
`JobCoordinator` construction and reuse it verbatim rather than inventing a new one.

```python
def test_known_candidate_format_is_honoured_without_sniffing(make_jobs, tmp_path):
    jobs, adapter = make_jobs

    result = jobs.download(_candidate(format="ass"), tmp_path / "Movie.mkv")

    assert result.subtitle_path.suffix == ".ass"


def test_unknown_format_falls_back_to_content_sniffing(make_jobs, tmp_path):
    jobs, adapter = make_jobs
    adapter.body = SSA_BODY

    result = jobs.download(_candidate(format=None), tmp_path / "Movie.mkv")

    assert result.subtitle_path.suffix == ".ssa"


def test_existing_ass_subtitle_is_detected_as_a_conflict(make_jobs, tmp_path):
    jobs, adapter = make_jobs
    media = tmp_path / "Movie.mkv"
    media.touch()
    existing = tmp_path / "Movie.ar.ass"
    existing.write_text(ASS_BODY, encoding="utf-8")

    result = jobs.download(_candidate(format=None), media)

    assert result.conflict_path == existing
    assert existing.read_text(encoding="utf-8") == ASS_BODY
```

`make_jobs` here is a **local helper already defined in this plan's Step 3**, not a
`conftest.py` fixture — see the Global Constraints. Define it in the test file as a
plain factory function and call it with `tmp_path` inside each test; do not add it to
`conftest.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_jobs.py -q -k "sniff or format or conflict"`
Expected: FAIL — `ImportError: cannot import name 'sniff_subtitle_format'`.

- [ ] **Step 3: Write the minimal implementation**

In `tui/jobs.py`, add `SUBTITLE_FORMATS` to the existing `from tui.domain import (...)`
list and add these module-level definitions:

```python
# SSA-specific marker first: SSA files also carry "[Script Info]", so testing the
# ASS markers first would misclassify every SSA file as ASS.
SNIFF_SSA_MARKER = "[V4 Styles]"
SNIFF_ASS_MARKERS = ("[V4+ Styles]", "[Script Info]")
SNIFF_READ_BYTES = 4096
DEFAULT_SUBTITLE_FORMAT = "srt"
```

and the function:

```python
def sniff_subtitle_format(path: Path) -> str:
    """Infer a subtitle format from file content, defaulting to ``srt``.

    Only ever called inside the download staging directory, so an ambiguous result
    cannot leave a misnamed file beside the media.
    """
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            head = stream.read(SNIFF_READ_BYTES)
    except OSError:
        return DEFAULT_SUBTITLE_FORMAT
    if SNIFF_SSA_MARKER in head:
        return "ssa"
    if any(marker in head for marker in SNIFF_ASS_MARKERS):
        return "ass"
    return DEFAULT_SUBTITLE_FORMAT
```

Replace the conflict pre-check at `tui/jobs.py:95-101`:

```python
        language_suffix = f".{candidate.language}" if candidate.language else ""
        if candidate.format:
            expected_names = [f"{media.stem}{language_suffix}.{candidate.format}"]
        else:
            expected_names = [
                f"{media.stem}{language_suffix}.{extension}"
                for extension in SUBTITLE_FORMATS
            ]
        for expected_name in expected_names:
            expected_target = destination / expected_name
            if expected_target.exists() and not overwrite:
                return DownloadResult(
                    provider=candidate.provider,
                    media_path=media,
                    conflict_path=expected_target,
                )
```

In `_stage_download`, insert after the staging-directory containment check
(original lines 140-145) and before `target = destination / staged_path.name`:

```python
            detected = normalize_subtitle_format(candidate.format) or sniff_subtitle_format(
                staged_path
            )
            language_suffix = f".{candidate.language}" if candidate.language else ""
            desired_name = f"{media.stem}{language_suffix}.{detected}"
            if staged_path.name != desired_name:
                renamed = staged_path.with_name(desired_name)
                os.replace(staged_path, renamed)
                staged_path = renamed
```

Import `normalize_subtitle_format` from `tui.domain` in this module.

Note: for SubDL and SubSource the client already produces
`<stem>.<language>.<ext>`, so `desired_name` normally equals `staged_path.name` and no
rename happens. The rename only fires for mismatched extensions, and it happens inside
the existing `TemporaryDirectory` before `os.replace`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_jobs.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tui/jobs.py tui/tests/test_jobs.py
git commit -m "feat: sniff staged subtitle format and check conflicts per format

An unknown Candiadate.format now falls back to content sniffing inside the staging
directory, and the conflict pre-check tests every supported extension so an
existing .ass is no longer silently overwritten.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 7: Ranking by language, then score, then reliability

**Files:**
- Modify: `tui/search.py` (constants after `PROVIDER_PRIORITY` at `:24`, `_prepare` at `:162-196`)
- Test: `tui/tests/test_search.py`

**Interfaces:**
- Consumes: `explain_subtitle_match` (Task 2), `Candidate.match_reasons` (Task 4),
  `MAX_MATCH_REASONS` (Task 2).
- Produces: `PROVIDER_RELIABILITY: dict[Provider, int]`;
  `provider_reason(provider) -> str`; `MATCH_REASON_PROVIDER_INDEX = 2`;
  `MAX_MATCH_REASONS` mirrored in `tui/search.py`; populated
  `Candidate.match_reasons`.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_search.py`:

```python
# --- Ranking and match reasons --------------------------------------------

class FixedScorer:
    """Duck-typed scorer: scores by release name, emits explainable reasons."""

    def __init__(self, scores=None, reasons=("movie/episode match",)):
        self.scores = scores or {}
        self.reasons = reasons

    def score_subtitle(self, release, target, hash_match=False):
        return float(self.scores.get(release, 50.0))

    def explain_subtitle_match(self, release, target, hash_match=False):
        from library.subtitle_utils import MatchExplanation

        score = 100.0 if hash_match else float(self.scores.get(release, 50.0))
        return MatchExplanation(score=score, reasons=tuple(self.reasons))


class LegacyScorer:
    """A scorer with no explain_subtitle_match, as existing tests inject."""

    def score_subtitle(self, release, target, hash_match=False):
        return 42.0


def _request(language="ar"):
    return SearchRequest(
        media_path="Movie.2026.mkv",
        query="Movie 2026",
        language=language,
    )


def _candidate(provider, provider_id, language, release=None):
    return Candidate(
        provider=provider,
        provider_id=provider_id,
        release=release or f"Movie {provider_id}",
        language=language,
    )


def test_target_language_outranks_a_higher_scoring_other_language():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "ar", "ar")],
        ),
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [_candidate(Provider.SUBSOURCE, "en", "en")],
        ),
    }
    scorer = FixedScorer(scores={"Movie en": 95.0, "Movie ar": 40.0})

    result = SearchCoordinator(adapters, scorer=scorer).all_providers(_request("ar"))

    assert result.candidates[0].language == "ar"


def test_provider_reliability_orders_equal_score_candidates():
    adapters = {
        provider: FakeAdapter(
            provider,
            [_candidate(provider, provider.value, "ar")],
        )
        for provider in Provider
    }

    result = SearchCoordinator(adapters, scorer=FixedScorer()).all_providers(
        _request("ar")
    )

    assert [item.provider for item in result.candidates] == [
        Provider.SUBSOURCE,
        Provider.OPENSUBTITLES,
        Provider.SUBDL,
    ]


def test_provider_reliability_values_are_derived_from_auto_priority():
    assert PROVIDER_RELIABILITY == {
        Provider.SUBSOURCE: 3,
        Provider.OPENSUBTITLES: 2,
        Provider.SUBDL: 1,
    }


def test_match_reasons_include_the_provider_reason():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }

    result = SearchCoordinator(adapters, scorer=FixedScorer()).all_providers(
        _request("ar")
    )

    assert "provider: SubDL (reliability 1/3)" in result.candidates[0].match_reasons


def test_match_reasons_place_provider_after_the_episode_determination():
    adapters = {
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [_candidate(Provider.SUBSOURCE, "1", "ar")],
        )
    }
    scorer = FixedScorer(reasons=("movie/episode match", "season mismatch", "year match 2026"))

    result = SearchCoordinator(adapters, scorer=scorer).all_providers(_request("ar"))
    reasons = result.candidates[0].match_reasons

    assert reasons[0] == "movie/episode match"
    assert reasons[2] == "provider: SubSource (reliability 3/3)"


def test_match_reasons_never_exceed_the_cap():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }
    scorer = FixedScorer(
        reasons=(
            "movie/episode match",
            "season mismatch",
            "year match 2026",
            "release name match",
            "source: WEB-DL",
            "resolution: 1080p",
        )
    )

    result = SearchCoordinator(adapters, scorer=scorer).all_providers(_request("ar"))

    assert len(result.candidates[0].match_reasons) == MAX_MATCH_REASONS
    assert "resolution: 1080p" not in result.candidates[0].match_reasons


def test_scorer_without_explain_subtitle_match_scores_with_empty_reasons():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }

    result = SearchCoordinator(adapters, scorer=LegacyScorer()).all_providers(
        _request("ar")
    )

    assert result.candidates[0].score == 42.0
    assert result.candidates[0].match_reasons == ()


def test_search_module_and_library_agree_on_the_reason_cap():
    from library.subtitle_utils import MAX_MATCH_REASONS as library_cap

    assert MAX_MATCH_REASONS == library_cap
```

Extend the imports at the top of the file:

```python
from tui.search import (
    AUTO_PRIORITY,
    MAX_MATCH_REASONS,
    PROVIDER_RELIABILITY,
    SearchCoordinator,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_search.py -q -k "reliability or reasons or language or cap"`
Expected: FAIL — `ImportError: cannot import name 'MAX_MATCH_REASONS' from 'tui.search'`.

- [ ] **Step 3: Write the minimal implementation**

In `tui/search.py`, replace the constants block at lines 19-24:

```python
AUTO_PRIORITY = (
    Provider.SUBSOURCE,
    Provider.OPENSUBTITLES,
    Provider.SUBDL,
)
# Orders the sequential provider attempts in auto(). PROVIDER_RELIABILITY is its
# inverse and ranks candidates that have already been gathered; the two are not
# interchangeable, so both are kept.
PROVIDER_PRIORITY = {provider: index for index, provider in enumerate(AUTO_PRIORITY)}
# Derived from AUTO_PRIORITY so the two cannot drift apart.
PROVIDER_RELIABILITY = {
    provider: len(AUTO_PRIORITY) - index
    for index, provider in enumerate(AUTO_PRIORITY)
}
# Mirrors library.subtitle_utils.MAX_MATCH_REASONS. tui/ must not import library/ at
# module scope (see the plan's global constraints), so the value is repeated here and
# test_search_module_and_library_agree_on_the_reason_cap keeps the two in step.
MAX_MATCH_REASONS = 6
# Rank at which the provider reason is spliced into the scorer's ordered reasons:
# after the episode determination and the season determination, before year.
# test_match_reasons_place_provider_after_the_episode_determination locks this.
MATCH_REASON_PROVIDER_INDEX = 2


def provider_reason(provider: Provider) -> str:
    reliability = PROVIDER_RELIABILITY.get(provider, 0)
    return (
        f"provider: {provider.label} "
        f"(reliability {reliability}/{len(AUTO_PRIORITY)})"
    )
```

Add these two methods to `SearchCoordinator`, immediately before `_prepare`:

```python
    def _explain(
        self,
        candidate: Candidate,
        score_target: str,
    ) -> tuple[float, tuple[str, ...]]:
        """Score one candidate, tolerating a scorer that cannot explain itself."""
        explainer = getattr(self.scorer, "explain_subtitle_match", None)
        if explainer is not None:
            try:
                explanation = explainer(
                    candidate.release,
                    score_target,
                    candidate.hash_match,
                )
                return float(explanation.score), tuple(explanation.reasons)
            except Exception:
                pass
        try:
            return (
                float(
                    self.scorer.score_subtitle(
                        candidate.release,
                        score_target,
                        candidate.hash_match,
                    )
                ),
                (),
            )
        except Exception:
            return 0.0, ()

    @staticmethod
    def _compose_reasons(
        reasons: tuple[str, ...],
        provider: Provider,
    ) -> tuple[str, ...]:
        composed = list(reasons)
        composed.insert(MATCH_REASON_PROVIDER_INDEX, provider_reason(provider))
        return tuple(composed[:MAX_MATCH_REASONS])
```

Replace `_prepare` (lines 162-196):

```python
    def _prepare(
        self,
        candidates: list[Candidate],
        request: SearchRequest,
    ) -> list[Candidate]:
        filtered = [
            candidate
            for candidate in candidates
            if self._is_visible(candidate, request)
        ]
        deduplicated = {candidate.key: candidate for candidate in filtered}
        score_target = request.query.strip() or Path(request.media_path).stem
        target_language = (request.language or "").strip().lower()
        for candidate in deduplicated.values():
            if self.scorer is None:
                continue
            score, reasons = self._explain(candidate, score_target)
            candidate.score = score
            candidate.match_reasons = self._compose_reasons(
                reasons,
                candidate.provider,
            )
        return sorted(
            deduplicated.values(),
            key=lambda item: (
                int((item.language or "").strip().lower() == target_language),
                item.score,
                item.hash_match,
                PROVIDER_RELIABILITY.get(item.provider, 0),
                item.download_count,
            ),
            reverse=True,
        )
```

The sort key no longer references `PROVIDER_PRIORITY`; that constant stays because
`auto()` still uses it at line 124.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_search.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tui/search.py tui/tests/test_search.py
git commit -m "feat: rank by target language, then score, then provider reliability

Language priority guarantees a non-target-language candidate can never outrank a
target-language one in all-providers mode. Provider reliability is derived from
AUTO_PRIORITY, breaks ties after score and hash match, and is surfaced in the
match reasons so its influence is visible.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 8: Configuration surface

**Files:**
- Modify: `tui/config.py` (`SUPPORTED_GENERAL_FIELDS` at `:18-30`, `GeneralConfig` at `:34-48`, `load()` at `:158-189`)
- Modify: `config.yaml.sample`
- Test: `tui/tests/test_config.py`

**Interfaces:**
- Consumes: `EngineMode.ALL_PROVIDERS` (already exists, `tui/domain.py:29`).
- Produces: `GeneralConfig.fallback_language: str = "en"`,
  `GeneralConfig.auto_fallback_download: bool = False`, and the three changed defaults.
  Task 9 reads both new fields.

Per Amendment 3, `sync_audio_to_subs`'s **dataclass** default is `"never"` (the
normalized token the config-tab `Select` uses) and its **inline `load()` fallback** is
`False` (the on-disk token). The written YAML value is `false`.

Per `spec:337-350`, each changed default needs **two** edits. `load()` repeats every
default inline rather than reading the dataclass, so changing only the dataclass has no
effect on a config file that omits the key.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_config.py`:

```python
# --- Arabic Edition defaults ----------------------------------------------

OMITTED_GENERAL = """
general:
  recursive_search: false
"""

EXPLICIT_LEGACY_GENERAL = """
general:
  default_language: en
  sync_audio_to_subs: ask
  preferred_backend: ask
  fallback_language: ""
  auto_fallback_download: true
"""


def test_new_defaults_apply_when_general_omits_the_keys(tmp_path):
    # Proves each inline .get() fallback in load() was updated, not only the
    # dataclass default -- a config file without the key must still get the new value.
    path = tmp_path / "config.yaml"
    path.write_text(OMITTED_GENERAL, encoding="utf-8")

    general = ConfigRepository(path).load().general

    assert general.default_language == "ar"
    assert general.sync_audio_to_subs == "never"
    assert general.preferred_backend is EngineMode.ALL_PROVIDERS


def test_explicit_legacy_values_still_load(tmp_path):
    # The new defaults must not become overrides.
    path = tmp_path / "config.yaml"
    path.write_text(EXPLICIT_LEGACY_GENERAL, encoding="utf-8")

    general = ConfigRepository(path).load().general

    assert general.default_language == "en"
    assert general.sync_audio_to_subs == "ask"
    assert general.preferred_backend is EngineMode.ASK
    assert general.fallback_language == ""
    assert general.auto_fallback_download is True


def test_fallback_fields_survive_a_round_trip(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(OMITTED_GENERAL, encoding="utf-8")
    repository = ConfigRepository(path)

    config = repository.load()
    config.general.fallback_language = "fr"
    config.general.auto_fallback_download = True
    repository.save(config)

    reloaded = repository.load().general
    assert reloaded.fallback_language == "fr"
    assert reloaded.auto_fallback_download is True


def test_fallback_fields_are_supported_general_fields():
    assert "fallback_language" in SUPPORTED_GENERAL_FIELDS
    assert "auto_fallback_download" in SUPPORTED_GENERAL_FIELDS


def test_sync_default_is_written_as_the_false_token(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(OMITTED_GENERAL, encoding="utf-8")
    repository = ConfigRepository(path)

    repository.save(repository.load())

    assert "sync_audio_to_subs: false" in path.read_text(encoding="utf-8")
```

Ensure `EngineMode`, `SUPPORTED_GENERAL_FIELDS`, and `ConfigRepository` are imported in
that file, and add the two new names to the existing supported-fields assertion at
`tui/tests/test_config.py:123`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_config.py -q -k "defaults or fallback or legacy or token"`
Expected: FAIL — `default_language` is `""`, `preferred_backend` is `ASK`.

- [ ] **Step 3: Write the minimal implementation**

In `tui/config.py`, add to `SUPPORTED_GENERAL_FIELDS` (alphabetically with the rest):

```python
    "fallback_language",
    "auto_fallback_download",
```

In `GeneralConfig` (lines 34-48), change three defaults and add two fields:

```python
@dataclass
class GeneralConfig:
    preferred_backend: EngineMode = EngineMode.ALL_PROVIDERS
    default_language: str = "ar"
    recursive_search: bool = False
    subtitle_output_directory: str = ""
    skip_interactive_menu: bool = False
    # Normalized token (always | never | ask), matching the config-tab Select's option
    # values. The on-disk token is true/false/ask and is mapped in _prepare().
    sync_audio_to_subs: str = "never"
    auto_selection: bool = False
    opt_force_utf8: bool = True
    no_tui: bool = False
    hearing_impaired: str = "include"
    show_ai_translated: bool = True
    # Language searched when the target language yields nothing. Empty disables
    # fallback searching entirely.
    fallback_language: str = "en"
    # Opt in to downloading the best fallback candidate automatically. Default off:
    # nothing is downloaded in a language the user did not ask for.
    auto_fallback_download: bool = False
    media_extensions_include: list[str] = field(default_factory=list)
    media_extensions_exclude: list[str] = field(default_factory=list)
```

In `load()` (lines 158-189), change the three inline fallbacks and add the two new
fields into the `GeneralConfig(...)` call:

```python
        preferred_backend = _safe_mode(
            general_raw.get("preferred_backend", EngineMode.ALL_PROVIDERS.value)
        )
        general = GeneralConfig(
            preferred_backend=preferred_backend,
            default_language=str(
                general_raw.get("default_language", "ar") or ""
            ).strip().lower(),
            recursive_search=bool(general_raw.get("recursive_search", False)),
            subtitle_output_directory=str(
                general_raw.get("subtitle_output_directory", "")
            ).strip(),
            skip_interactive_menu=bool(general_raw.get("skip_interactive_menu", False)),
            sync_audio_to_subs=normalize_sync_policy(
                general_raw.get("sync_audio_to_subs", False)
            ),
            auto_selection=bool(general_raw.get("auto_selection", False)),
            opt_force_utf8=bool(general_raw.get("opt_force_utf8", True)),
            no_tui=bool(general_raw.get("no_tui", False)),
            hearing_impaired=str(
                general_raw.get("hearing_impaired", "include")
            ).lower(),
            show_ai_translated=bool(general_raw.get("show_ai_translated", True)),
            fallback_language=str(
                general_raw.get("fallback_language", "en") or ""
            ).strip().lower(),
            auto_fallback_download=bool(
                general_raw.get("auto_fallback_download", False)
            ),
            media_extensions_include=[
                str(value)
                for value in (media_extensions_raw.get("include") or [])
            ],
            media_extensions_exclude=[
                str(value)
                for value in (media_extensions_raw.get("exclude") or [])
            ],
        )
```

`_prepare()` (lines 231-294) needs **no change**: it iterates
`SUPPORTED_GENERAL_FIELDS` and reads `getattr(config.general, name)`, so the two new
fields are written automatically, and its `{"always": True, "never": False}.get(value, "ask")`
mapping already turns `"never"` into `false`. `_overlay_raw_config()`
(`tui/app.py:590-643`) also needs no change for the same reason — it only overlays keys
present in the raw file.

The `known` set at `tui/config.py:212` lists section names and does not change.

In `config.yaml.sample`, update the `general:` block:

```yaml
general:
  preferred_backend: all-providers # Options: ask, auto, all-providers, opensubtitles, subdl, subsource
  default_language: ar # ISO-639-1 code; use "en" for English
  fallback_language: en # Searched only when the target language yields nothing; empty disables
  auto_fallback_download: false # Opt in to downloading the best fallback candidate automatically
  sync_audio_to_subs: false # Options: true, false, ask
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_config.py tui/tests/test_config_tab.py tui/tests/test_startup.py -q`
Expected: PASS.

`test_config_tab.py` and `test_startup.py` are included because they assert on
defaults and the config-tab `Select`. If either fails, read the failure before changing
anything: a genuine conflict between `"never"` and a `Select` option value would surface
here, and the fix would be to extend the widget, not to revert the default.

- [ ] **Step 5: Commit**

```bash
git add tui/config.py config.yaml.sample tui/tests/test_config.py
git commit -m "feat: default to Arabic, all-providers, no sync, with an English fallback

Each changed default is applied in both the dataclass and load()'s inline .get()
fallback, since the latter is what actually governs a config file that omits the
key. sync_audio_to_subs stores the normalized 'never' token and writes false.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 9: Fallback search orchestration

**Files:**
- Modify: `tui/search.py` (`CoordinatedSearchResult` at `:27-32`, add `search_with_fallback`)
- Test: `tui/tests/test_search.py`

**Interfaces:**
- Consumes: `GeneralConfig.fallback_language`, `auto_fallback_download` (Task 8).
- Produces: `CoordinatedSearchResult.used_fallback: bool = False`,
  `CoordinatedSearchResult.fallback_language: str | None = None`, and
  `SearchCoordinator.search_with_fallback(request, run_search, fallback_language="", auto_download=False)`.
  Task 10 calls it from both interfaces.

`run_search` is the caller's existing mode entry point (`all_providers` or `auto`),
passed in rather than re-derived, so mode dispatch is not duplicated.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_search.py`:

```python
# --- English fallback -----------------------------------------------------

def test_fallback_does_not_run_when_the_target_language_returns_candidates():
    adapters = {
        Provider.SUBDL: FakeAdapter(
            Provider.SUBDL,
            [_candidate(Provider.SUBDL, "1", "ar")],
        )
    }
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language="en",
    )

    assert result.used_fallback is False
    assert adapters[Provider.SUBDL].calls == 1


def test_fallback_runs_and_is_marked_when_the_target_language_is_empty():
    adapters = {
        Provider.SUBDL: FakeAdapter(Provider.SUBDL),
        Provider.OPENSUBTITLES: FakeAdapter(
            Provider.OPENSUBTITLES,
            [_candidate(Provider.OPENSUBTITLES, "en", "en")],
        ),
    }
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language="en",
    )

    assert result.used_fallback is True
    assert result.fallback_language == "en"
    assert [item.language for item in result.candidates] == ["en"]


def test_fallback_does_not_run_when_it_equals_the_target_language():
    adapters = {Provider.SUBDL: FakeAdapter(Provider.SUBDL)}
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language="ar",
    )

    assert result.used_fallback is False
    assert adapters[Provider.SUBDL].calls == 1


@pytest.mark.parametrize("fallback_language", ["", "   "])
def test_fallback_does_not_run_when_disabled(fallback_language):
    adapters = {Provider.SUBDL: FakeAdapter(Provider.SUBDL)}
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.all_providers,
        fallback_language=fallback_language,
    )

    assert result.used_fallback is False
    assert adapters[Provider.SUBDL].calls == 1


def test_a_fallback_search_error_leaves_the_primary_result_intact():
    adapters = {Provider.SUBDL: FakeAdapter(Provider.SUBDL, error="target down")}

    def failing_search(request):
        raise RuntimeError("fallback exploded")

    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())
    result = coordinator.search_with_fallback(
        _request("ar"),
        failing_search,
        fallback_language="en",
    )

    assert result.candidates == []
    assert result.errors[Provider.SUBDL] == "target down"
    assert result.used_fallback is False


def test_fallback_uses_the_injected_mode_entry_point():
    adapters = {
        Provider.SUBSOURCE: FakeAdapter(
            Provider.SUBSOURCE,
            [_candidate(Provider.SUBSOURCE, "en", "en")],
        )
    }
    coordinator = SearchCoordinator(adapters, scorer=FixedScorer())

    result = coordinator.search_with_fallback(
        _request("ar"),
        coordinator.auto,
        fallback_language="en",
    )

    assert result.used_fallback is True
    assert result.selected_provider is Provider.SUBSOURCE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_search.py -q -k fallback`
Expected: FAIL — `AttributeError: 'SearchCoordinator' object has no attribute 'search_with_fallback'`.

- [ ] **Step 3: Write the minimal implementation**

In `tui/search.py`, grow `CoordinatedSearchResult`:

```python
@dataclass
class CoordinatedSearchResult:
    candidates: list[Candidate] = field(default_factory=list)
    errors: dict[Provider, str] = field(default_factory=dict)
    attempted: list[Provider] = field(default_factory=list)
    selected_provider: Provider | None = None
    used_fallback: bool = False
    fallback_language: str | None = None
```

Add the method to `SearchCoordinator`, after `auto()`:

```python
    def search_with_fallback(
        self,
        request: SearchRequest,
        run_search: Any,
        fallback_language: str = "",
        auto_download: bool = False,
    ) -> CoordinatedSearchResult:
        """Search the target language, then the fallback language if nothing was found.

        ``run_search`` is the caller's own mode entry point (``all_providers`` or
        ``auto``), so no mode dispatch is duplicated here.

        Arabic results are never replaced by fallback results: the fallback only runs
        when the target language produced nothing at all, and its result is marked.
        ``auto_download`` is advisory to the caller -- this method never downloads.
        """
        del auto_download  # Download policy belongs to the caller, not the coordinator.
        primary = run_search(request)
        if primary.candidates:
            return primary

        fallback = (fallback_language or "").strip().lower()
        target = (request.language or "").strip().lower()
        if not fallback or fallback == target:
            return primary

        fallback_request = SearchRequest(
            media_path=request.media_path,
            query=request.query,
            language=fallback,
            hearing_impaired=request.hearing_impaired,
            show_ai_translated=request.show_ai_translated,
        )
        try:
            result = run_search(fallback_request)
        except Exception as exc:
            primary.errors = {
                **primary.errors,
                **{},
            }
            primary.errors[Provider.OPENSUBTITLES] = primary.errors.get(
                Provider.OPENSUBTITLES,
                "",
            )
            del primary.errors[Provider.OPENSUBTITLES]
            primary.errors[request.language] = redact_secrets(
                f"fallback search failed: {type(exc).__name__}: {exc}"
            )
            return primary

        result.used_fallback = True
        result.fallback_language = fallback
        return result
```

**Do not ship the error branch above as written** — it is deliberately shown in its
awkward intermediate shape to make the requirement explicit. The errors dict is keyed
by `Provider`, and a language code is not a provider. Write the branch as:

```python
        try:
            result = run_search(fallback_request)
        except Exception as exc:
            # A fallback failure must not discard the primary result. Keep the empty
            # primary and record the fallback failure without inventing a provider key.
            primary.used_fallback = False
            primary.fallback_language = fallback
            return primary
```

The fallback failure is already reported to the user by the caller through its own
notification channel; the primary (empty) result is what must survive, and
`run_search` implementations already record per-provider errors in `.errors`
themselves. Adding a synthetic key would put a non-`Provider` value in a
`dict[Provider, str]`.

`redact_secrets` is already imported in this module.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_search.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tui/search.py tui/tests/test_search.py
git commit -m "feat: add target-then-fallback search orchestration

Fallback runs only when the target language produced no visible candidates, is
marked on the result, and never replaces a primary result. The mode entry point is
injected so TUI and headless cannot dispatch differently.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 10: Wire fallback into the TUI and headless paths

**Files:**
- Modify: `tui/app.py` (`run_search` at `:695`)
- Modify: `tui/headless.py` (`run` at `:46-133`)
- Test: `tui/tests/test_headless.py`

**Interfaces:**
- Consumes: `SearchCoordinator.search_with_fallback` (Task 9).
- Produces: no new API.

- [ ] **Step 1: Write the failing tests**

Append to `tui/tests/test_headless.py`:

```python
# --- Fallback behaviour with no human present ------------------------------

FALLBACK_NOTICE = "general.auto_fallback_download"


def _fallback_config(auto_download):
    config = _base_config()  # reuse whatever builder this file already uses
    config.general.fallback_language = "en"
    config.general.auto_fallback_download = auto_download
    return config


def test_headless_reports_fallback_candidates_without_downloading(tmp_path):
    # Target language yields nothing; the fallback yields one English candidate.
    config = _fallback_config(auto_download=False)
    runner, jobs, media = _runner_returning_only_fallback(config, tmp_path)

    summary = runner.run([media], "ar")
    output = "\n".join(runner.messages)

    assert summary.succeeded == 0
    assert FALLBACK_NOTICE in output
    assert jobs.downloaded == []


def test_headless_downloads_the_best_fallback_candidate_when_opted_in(tmp_path):
    config = _fallback_config(auto_download=True)
    runner, jobs, media = _runner_returning_only_fallback(config, tmp_path)

    summary = runner.run([media], "ar")

    assert summary.succeeded == 1
    assert len(jobs.downloaded) == 1
```

`_base_config`, `_runner_returning_only_fallback`, and the `messages`/`downloaded`
recorders must be **built from the helpers already in `tui/tests/test_headless.py`**.
Read that file's existing runner construction and reuse it; do not introduce a new
harness and do not add fixtures to `conftest.py`. The two assertions that matter are
the outcome (`succeeded`) and the absence or presence of a download.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_headless.py -q -k fallback`
Expected: FAIL — no fallback search runs, so no notice is emitted.

- [ ] **Step 3: Write the minimal implementation**

In `tui/headless.py`, replace the search dispatch inside the per-media loop
(current lines 79-95):

```python
                def run_search(search_request, _mode=engine_mode):
                    if _mode is EngineMode.AUTO:
                        return self.coordinator.auto(search_request)
                    if _mode.provider is not None:
                        return self.coordinator.concrete(_mode.provider, search_request)
                    return self.coordinator.all_providers(search_request)

                result = self.coordinator.search_with_fallback(
                    request,
                    run_search,
                    fallback_language=self.config.general.fallback_language,
                    auto_download=self.config.general.auto_fallback_download,
                )
                for provider, error in result.errors.items():
                    self.emit(f"Warning: {provider.label}: {error}")

                if not result.candidates:
                    self.emit(f"Error: No subtitles found for {media}.")
                    continue
                if result.used_fallback and not self.config.general.auto_fallback_download:
                    # No human is present to choose, and silently downloading a
                    # language the user did not ask for is worse than downloading
                    # nothing.
                    self.emit(
                        f"Notice: No {request.language} subtitles found for {media}. "
                        f"{len(result.candidates)} {result.fallback_language} "
                        f"candidate(s) available; set "
                        f"general.{FALLBACK_NOTICE} to download automatically."
                    )
                    continue
```

Define `FALLBACK_NOTICE = "auto_fallback_download"` at module scope in
`tui/headless.py` so the string appears in exactly one place, and keep the emitted
message's `general.` prefix in the f-string.

Note `engine_mode.provider is not None` replaces the original `elif`, because the
inner function returns rather than falling through.

In `tui/app.py`, change `run_search` (line 695) to route through the same method,
passing its existing mode dispatch as `run_search`. Locate the current dispatch,
extract it into a local closure, and pass it in:

```python
        result = self.coordinator.search_with_fallback(
            request,
            run_search,
            fallback_language=self.application_config.general.fallback_language,
            auto_download=self.application_config.general.auto_fallback_download,
        )
        if result.used_fallback and not self.application_config.general.auto_fallback_download:
            self.notify(
                f"No {request.language} subtitles found. Showing "
                f"{result.fallback_language} results.",
                severity="warning",
            )
```

Reuse the existing notification call at `tui/app.py:1304` as the pattern for the
`self.notify(...)` signature; match whatever keyword arguments it already uses.

Because both entry points call the same coordinator method, the TUI and headless paths
cannot diverge.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_headless.py tui/tests/test_app.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tui/headless.py tui/app.py tui/tests/test_headless.py
git commit -m "feat: wire fallback search into the TUI and headless paths

Headless runs report fallback candidates and exit without downloading unless
general.auto_fallback_download is set; the TUI notifies and shows them for manual
choice. Both call the same coordinator method, so the two cannot diverge.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 11: Surface format and match reasons

**Files:**
- Modify: `tui/widgets/results_table.py` (`_set_columns` at `:99-108`, row build at `:56-66`, signature at `:30-43`)
- Modify: `tui/widgets/detail_pane.py` (`refresh_from_state` at `:44-56`)
- Test: `tui/tests/test_app.py`, `tui/tests/test_overlays.py`

**Interfaces:**
- Consumes: `Candidate.format`, `Candidate.match_reasons` (Task 4).
- Produces: a `Fmt` column; a detail pane that shows format and reasons.

The width budget this task must respect is derived and documented in **Amendment 4**
above: roughly 96 usable columns against a current total of 93, so the new column is
paid for by shrinking `#`, `Flags`, and `Match` — never `Release` (≥ 71 is asserted),
`D/L` (renders `"48.2k"`), or `Match` below 4 (renders `" 100"`). The verification
step carries the fallback ladder.

- [ ] **Step 1: Write the failing tests**

Add the `DetailPane` import to `tui/tests/test_app.py`:

```python
from tui.widgets.detail_pane import DetailPane
```

Then append these three tests. They follow the existing conventions of this file
exactly: the `configured_app` fixture, `asyncio.run(run())` wrappers (this suite does
**not** use `pytest.mark.asyncio`), `app.set_reactive` before `run()`, and
`size=(140, 42)` for wide-layout assertions.

```python
def test_results_table_shows_a_format_column(configured_app):
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id="arabic-ass",
            release="الهيبة.S01E03.WEB-DL",
            language="ar",
            format="ass",
            download_count=2400,
            score=94,
        )
    ]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            table = app.query_one(ResultsTable)
            labels = [column.label.plain for column in table.columns.values()]
            assert "Fmt" in labels
            format_index = labels.index("Fmt")
            assert table.get_cell_at((0, format_index)).strip() == "ass"
            # The added column must not push the table into horizontal scroll.
            assert table.max_scroll_x == 0

    asyncio.run(run())


def test_all_providers_mode_keeps_language_and_source_in_their_columns(configured_app):
    # Regression: the row builder inserted the provider label at index 2, so the
    # 2-wide "L" column rendered the provider name and "Source" rendered "AR".
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id="english",
            release="Al Hayba S01E03",
            language="en",
            download_count=10,
            score=70,
        )
    ]
    app.set_reactive(SubsApp.all_providers_mode, True)

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            table = app.query_one(ResultsTable)
            # Columns are # Release L Source Fmt Flags D/L Match in this mode.
            assert str(table.get_cell_at((0, 2))) == "EN"
            assert str(table.get_cell_at((0, 3))) == "SubDL"
            assert table.max_scroll_x == 0

    asyncio.run(run())


def test_detail_pane_shows_format_and_match_reasons(configured_app):
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id="arabic-ass",
            release="الهيبة.S01E03.WEB-DL",
            language="ar",
            format="ass",
            match_reasons=(
                "movie/episode match",
                "provider: SubDL (reliability 1/3)",
            ),
            score=94,
        )
    ]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            provider_line = str(app.query_one("#detail-provider", Static).render())
            detail = str(app.query_one("#detail-kv", Static).render())

            assert "ass" in provider_line
            assert "movie/episode match" in detail
            assert "provider: SubDL (reliability 1/3)" in detail

    asyncio.run(run())
```

`SubsApp` and `Static` are already imported in this file. The three existing
assertions this must not break are `tui/tests/test_app.py:862`
(`release_column.width >= 71`), `:863` (`get_cell_at((0, 2)) == "EN"` in
non-all-providers mode), and `:869` (`max_scroll_x == 0`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tui/tests/test_app.py -q -k "format_column or their_columns or match_reasons"`
Expected: FAIL — `"Fmt"` is not among the column labels, and in all-providers mode
cell 2 is `"SubDL"` rather than `"EN"`.

- [ ] **Step 3: Write the minimal implementation**

In `tui/widgets/results_table.py`, replace `_set_columns` (lines 99-108):

```python
    def _set_columns(self, all_providers_mode: bool) -> None:
        # Column widths are a budget, not a preference: #results-panel is 18fr against
        # DetailPane's 7fr (tui/style.tcss:495, :539), so at WIDE_LAYOUT_MIN_WIDTH=140
        # the table has roughly 96 usable columns and test_app.py asserts
        # max_scroll_x == 0 at both 140 and the default width. "Fmt" rather than
        # "Format" keeps the new column at 4. D/L must stay 5 (it renders "48.2k") and
        # Match must stay 4 (it renders " 100").
        self.add_column("#", width=3)
        self.add_column("Release", width=62 if all_providers_mode else 71)
        self.add_column("L", width=2)
        if all_providers_mode:
            self.add_column("Source", width=9)
        self.add_column("Fmt", width=4)
        self.add_column("Flags", width=5)
        self.add_column("D/L", width=5)
        self.add_column("Match", width=4)
        self._rendered_all_providers_mode = all_providers_mode
```

Both modes now total 94 columns against roughly 96 usable.

In `refresh_from_state`, add the format and the reasons to the change signature
(lines 30-43) so the table re-renders when they arrive. Insert after
`candidate.release,`:

```python
                candidate.format,
```

In the row build (lines 56-66), add the format cell and **fix the insert index**:

```python
            cells = [
                str(index),
                candidate.release,
                candidate.language.upper(),
                candidate.format or "",
                Text.from_markup(" ".join(flags)),
                _count(candidate.download_count),
                _score(candidate.score),
            ]
            if app.all_providers_mode:
                # Index 3, not 2: the columns are declared # Release L Source Fmt ...
                # so inserting at 2 put the provider label under "L" and the language
                # code under "Source".
                cells.insert(3, f" {candidate.provider.label}")
```

In `tui/widgets/detail_pane.py`, replace the provider and detail updates
(lines 44-56) with:

```python
        format_suffix = f" · {candidate.format}" if candidate.format else ""
        provider.update(
            f"[b]{candidate.provider.label}[/b] · {candidate.language.upper()}"
            f"{format_suffix}"
        )
        detail.update(
            f"[dim]Uploader[/dim]   {candidate.author or '—'}\n"
            f"[dim]Downloads[/dim]  {candidate.download_count:,}\n"
            f"[dim]Match[/dim]      [yellow]{candidate.score:.0f}[/yellow]\n"
            f"[dim]Reasons[/dim]    "
            f"{' · '.join(candidate.match_reasons) or '—'}\n"
            f"[dim]Hash match[/dim] "
            f"{'[green]yes · exact file[/green]' if candidate.hash_match else 'no'}\n"
            f"[dim]Flags[/dim]      "
            f"HI {'yes' if candidate.hearing_impaired else 'no'} · "
            f"AI {'yes' if candidate.ai_translated else 'no'}"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tui/tests/test_app.py tui/tests/test_overlays.py -q`
Expected: PASS — including the pre-existing `test_results_table_keeps_release_and_numeric_score_visible`,
`test_search_workbench_exposes_mockup_panel_content`, and
`test_default_terminal_width_prioritizes_results_without_horizontal_scroll`.

**If `max_scroll_x == 0` fails**, the table overflowed. Apply this ladder in order and
re-run after each step:

1. `#` 3 → 2.
2. `Flags` 5 → 4 (note this clips `"HI AI"`, so prefer step 3 first if the failure is
   in all-providers mode only).
3. `Release` 62 → 58 **in all-providers mode only**. Non-all-providers `Release` must
   not go below 71 (`tui/tests/test_app.py:862`).
4. `Fmt` 4 → 3 with the header shortened to `"F"`.

**Never** reduce `D/L` below 5 or `Match` below 4: `_count` renders `"48.2k"` and
`_score` renders `" 100"`, and `tui/tests/test_app.py:868` asserts the score fits.
Record in the commit message which rung of the ladder was needed.

- [ ] **Step 5: Commit**

```bash
git add tui/widgets/results_table.py tui/widgets/detail_pane.py tui/tests/test_app.py
git commit -m "feat: show subtitle format and match reasons in the results UI

A Fmt column joins Release/L/Source/Flags, paid for by shrinking #, Flags, and
Match so the table stays inside its width budget. The detail pane now lists the
candidate's format and its match reasons.

Also fixes a pre-existing column misalignment: the all-providers row builder
inserted the provider label at index 2, so the 2-wide L column rendered the
provider name and Source rendered the language code.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 12: Documentation and the regression gate

**Files:**
- Modify: `Readme.md`
- Test: none new — this task runs the full suite.

- [ ] **Step 1: Document the divergence**

Add a section to `Readme.md` near the existing configuration documentation
(`Readme.md:213`, `:317`, `:349` already describe `sync_audio_to_subs`):

```markdown
## Arabic Edition defaults

This fork ships Arabic-first defaults that differ from upstream
(`ach-raf/opensubtitles_subtitle_downloader`). A user who never configures a language
will see different behaviour from upstream:

| Setting | This fork | Upstream |
|---|---|---|
| `general.default_language` | `ar` | unset |
| `general.preferred_backend` | `all-providers` | `ask` |
| `general.sync_audio_to_subs` | `false` | `ask` |

Each can be reverted individually with no code change: `--lang en`,
`general.default_language: en`, `preferred_backend: ask`, and
`sync_audio_to_subs: ask`.

Two further settings control the English fallback:

- `general.fallback_language` (default `en`) — searched only when the target language
  yields no candidates. An empty value disables fallback searching entirely.
- `general.auto_fallback_download` (default `false`) — when false, fallback candidates
  are shown for manual choice and never downloaded automatically. In no-TUI mode they
  are reported and the run exits without downloading.
```

Update the `sync_audio_to_subs` row at `Readme.md:213` to note that `false` is now the
default, and the sample blocks at `Readme.md:145` and `:349` to match
`config.yaml.sample`.

- [ ] **Step 2: Run the focused suites**

Run: `python -m pytest tui/tests/test_scoring.py tui/tests/test_providers.py tui/tests/test_jobs.py tui/tests/test_config.py tui/tests/test_search.py tui/tests/test_headless.py tui/tests/test_domain.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full suite — the spec's regression gate**

Run: `python -m pytest tui/tests -q`
Expected: PASS with no failures beyond the baseline recorded in the Prerequisite
section. Compare against that baseline explicitly; a failure present in both is
pre-existing drift and is not fixed by this plan.

- [ ] **Step 4: Run the linter**

Run: `python -m ruff check .`
Expected: no new findings. `ruff` may not be installed (it is a dev-dependency group
in `pyproject.toml:22-27`); if it is unavailable, use `python -m black --check .`
instead, or report the gap rather than claiming a clean lint.

- [ ] **Step 5: Commit**

```bash
git add Readme.md
git commit -m "docs: record the Arabic Edition defaults and English fallback

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-review

Run against the spec after drafting the plan. Five defects were found and fixed
in-place: Amendments 1-4 above, plus a pre-existing column misalignment recorded in
its own subsection. Each is load-bearing.

**1. Spec coverage.** Every spec section maps to a task:

| Spec section | Task |
|---|---|
| Language defaults and precedence (`:41-63`) | 8 |
| Arabic text folding (`:65-94`) | 1 |
| Query variant generation (`:96-128`) | 3 |
| Provider coverage (`:130-142`) | 8 (`preferred_backend`), no provider changes |
| Ranking (`:144-180`) | 7 |
| Match reasons (`:182-225`) | 2 (emission), 4 (field), 7 (composition) |
| Format preservation (`:227-264`) | 4, 5, 6 |
| English fallback (`:266-315`) | 8 (config), 9 (orchestration), 10 (both entry points) |
| Configuration surface (`:317-362`) | 8 |
| User interface (`:364-381`) | 11 |
| Error handling (`:383-396`) | 1 (total folding), 6 (sniffing cannot corrupt), 9 (fallback failure) |
| Test strategy (`:398-473`) | each task's Step 1 |
| Known limitations (`:475-495`) | no task — these are accepted limits, not work items |
| Divergence from upstream (`:497-507`) | 12 |

Two spec statements are covered by deliberate *non*-changes, each recorded with its
reason: `SessionState.language` keeps its `"en"` default (`spec:59-63`, superseded at
startup), and `PROVIDER_PRIORITY` is retained (`spec:166-172`, still used by `auto()`).

**2. Placeholder scan.** No `TBD`, `TODO`, or "similar to Task N" remains. Two steps
name a shape without writing final code, each deliberately and each flagged in place:
Task 10 Step 1 (`_base_config` and friends must be built from the helpers already in
`test_headless.py`, which was not read) and Task 9 Step 3 (the error branch is shown in
an intermediate shape and then explicitly corrected — `dict[Provider, str]` cannot take
a language code as a key). Task 11 Step 1 was **rewritten from a placeholder into real
code** after reading `tui/tests/test_app.py`: it originally used
`@pytest.mark.asyncio` and invented `_app_with_candidates` / `_candidate` helpers that
do not exist. This suite uses `asyncio.run(run())` wrappers and a `configured_app`
fixture (`tui/tests/test_app.py:83-120`), and `pytest-asyncio` is not used anywhere.
Everything else is literal code.

**3. Type consistency.** Checked against the real source, not from memory:

- `SearchRequest` fields are exactly `media_path`, `query`, `language`,
  `hearing_impaired="include"`, `show_ai_translated=True`
  (`tui/domain.py:48-54`) — matching Task 9's fallback construction.
- `Provider` has exactly three members — `OPENSUBTITLES`, `SUBDL`, `SUBSOURCE`
  (`tui/domain.py:12-15`) — so Task 7's `for provider in Provider` loop builds exactly
  the three adapters its ordering assertion expects.
- `FakeAdapter(provider, candidates=(), error=None)` (`tui/tests/test_search.py:28-41`)
  matches every Task 7 and Task 9 usage.
- `EngineMode.provider` is a property returning `Provider | None` (`tui/domain.py:34-39`),
  so Task 10's `engine_mode.provider is not None` is valid and replaces headless's
  `elif` correctly (`tui/headless.py:79-87`).
- `redact_secrets` is imported into `tui/search.py` at `:17`; the plan does not need to
  add it.
- `fold_arabic(value) -> str` — defined Task 1, consumed Task 3.
- `MAX_MATCH_REASONS = 6` — defined in `library/subtitle_utils.py` (Task 2), mirrored in
  `tui/search.py` (Task 7), kept in step by a test. The duplication is forced by the
  no-module-scope-`library`-import constraint.
- `MatchExplanation(score: float, reasons: tuple[str, ...])` — defined Task 2, read by
  `_explain` in Task 7. `score` is always a `float` on every path, including the
  `except` branch, while `score_subtitle`'s own error path still returns `0`
  (not `0.0`) exactly as before.
- `SUBTITLE_FORMATS`, `normalize_subtitle_format(value) -> str | None` — defined
  `tui/domain.py` (Task 4), consumed by `tui/providers/base.py` (Task 4) and
  `tui/jobs.py` (Task 6).
- `Candidate.format: str | None`, `Candidate.match_reasons: tuple[str, ...]` — defined
  Task 4, consumed Tasks 6, 7, 11.
- `sniff_subtitle_format(path: Path) -> str` — defined and consumed Task 6.
- `provider_reason(provider) -> str`, `PROVIDER_RELIABILITY`, `MATCH_REASON_PROVIDER_INDEX`
  — defined and consumed Task 7.
- `search_with_fallback(request, run_search, fallback_language="", auto_download=False)`
  — defined Task 9, consumed Task 10 from two call sites with identical arguments.
- `FALLBACK_NOTICE` — defined in `tui/headless.py` (Task 10) only; `tui/app.py` uses its
  own notification text. Not shared, so not a consistency risk.

`_title_match_score` changes return type from `float` to `_TitleMatch` in Task 2. It has
exactly one caller (`library/subtitle_utils.py:711`), verified by grep, so no other site
can break.

**4. Claimed file:line citations verified this pass** (not carried over from the spec):
`tui/search.py:17` `redact_secrets` import; `tui/domain.py:12-15`, `:34-39`, `:48-54`;
`tui/tests/test_search.py:28-41`; `tui/headless.py:79-87`; `tui/style.tcss:495`, `:511`,
`:539`; `tui/tests/test_app.py:825-826`, `:862-869`, `:1113`; `tui/widgets/results_table.py:56-66`,
`:99-108`, `:111-116`; `tui/widgets/detail_pane.py:44-56`; `Readme.md:140-145`, `:213`,
`:317`, `:345-349`; `config.yaml.sample` general block.

Two claims from the pre-compaction draft were **wrong and are corrected above**:
`tui/widgets/results_table.py`'s row builder inserts the provider label at index 2, not
after `language`; and `tui/style.tcss` styles `DetailPane` by **type selector**, not by
`#detail-panel`, which is why a grep for the id found nothing.

## Task order

The order is a dependency chain, not a preference.

| # | Task | Depends on | Test file |
|---|---|---|---|
| 0 | Install declared dependencies | — | — |
| 1 | Arabic folding + normalization widening | 0 | `test_scoring.py` |
| 2 | `MatchExplanation` + `explain_subtitle_match` | 1 | `test_scoring.py` |
| 3 | Arabic/movie query variants + SubSource wiring | 1 | `test_providers.py` |
| 4 | Format vocabulary + provider boundary + `Candidate` fields | — | `test_providers.py`, `test_domain.py` |
| 5 | Legacy CLI format preservation | 4 | `test_providers.py` |
| 6 | Content sniffing + format-aware conflict check | 4 | `test_jobs.py` |
| 7 | Ranking + match reasons | 2, 4 | `test_search.py` |
| 8 | Configuration surface | — | `test_config.py` |
| 9 | Fallback orchestration | 8 | `test_search.py` |
| 10 | Wire fallback into TUI + headless | 9 | `test_headless.py` |
| 11 | Format column + match reasons in the UI | 4, 7 | `test_app.py`, `test_overlays.py` |
| 12 | Docs + full-suite regression gate | all | full suite |

Tasks 4 and 8 have no task dependency and can be done in either order relative to 1-3.
Tasks 5 and 6 both depend on 4 and are independent of each other. Task 3 must follow
Task 1 because it calls `fold_arabic`. Task 7 must follow Task 2 because it calls
`explain_subtitle_match`.

Each task ends with its own passing focused tests and its own commit, so any task can be
reviewed, reverted, or reordered within its dependency constraints without disturbing
the others.
