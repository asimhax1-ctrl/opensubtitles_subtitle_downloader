# PROJECT_STATE — OSD Arabic Edition (handoff document)

> For a future AI session or developer. Read this first, then follow
> §10 (startup checklist). All claims below were verified against
> `git`, the test suite, or direct code reads in the audit session
> ending 2026-09-22. Anything uncertain is marked "unknown".

---

## 1. Project overview

- **What this fork is:** a working, mature fork of
  `ach-raf/opensubtitles_subtitle_downloader`, adapted into an
  **Arabic-first subtitle discovery and download tool**. The Arabic
  Edition was already implemented before the 2026-09-22 audit; the
  audit fixed bugs and strengthened tests, it did not rebuild anything.
- **Upstream repository:** `https://github.com/ach-raf/opensubtitles_subtitle_downloader.git`
  (`upstream` remote). **Never push to upstream.**
- **Main goal of the Arabic Edition:** make Arabic the default target
  language and make discovery of *existing* Arabic subtitles accurate:
  correct movie/episode identification, Arabic-aware matching and query
  variants, verified-compatibility ranking, format preservation
  (ASS/SSA/SRT/VTT/SUB), English only as an explicit non-silent
  fallback. No AI translation, no TMDB/metadata services, no GUI
  redesign, no UI localization/RTL — all explicitly out of scope.
- **Design source of truth:**
  `docs/superpowers/specs/2026-09-16-arabic-edition-design.md`
  (read it before changing language defaults, fallback flow, ranking,
  or format preservation). History/reference docs (untracked working
  files): `DEEPSEEK_REVIEW.md` (issue backlog H/M/L),
  `KIMI_IMPLEMENTATION_REPORT.md` (what prior sessions fixed).

---

## 2. Repository / Git state (as of 2026-09-22)

- **Branch:** `feat/arabic-edition` (all work happens here).
- **Origin (fork, push target):**
  `https://github.com/asimhax1-ctrl/opensubtitles_subtitle_downloader.git`
- **Upstream (read-only):** `https://github.com/ach-raf/opensubtitles_subtitle_downloader.git`
- **Latest commits** (`git log --oneline`, newest first):
  - `0207f78` chore(lint): make ruff check green (44 -> 0)
  - `563c77c` fix(formats): preserve .sub members in legacy archive downloads
  - `85038b3` fix(queries): collapse audio lines and strip groups before title hypotheses
  - `315d911` fix(queries): emit honest shapes for season-less episodes
  - `63f0542` fix(ranking): stop treating REMUX as part of the title
  - `ccabe28` fix(subdl): read subtitle id from absolute download URLs
  - `3d36c47` checkpoint: land prior sessions' verified review fixes and verification layer
- **Working tree:** clean except 6 pre-existing *untracked* session-report
  files (`AGENTS.md`, `BEFORE_KIMI_RESUME.diff`, `DEEPSEEK_REVIEW.md`,
  `KIMI_IMPLEMENTATION_REPORT.md`, `KIMI_RESUME_PROMPT.txt`,
  `KIMI_RESUME_RESULT.md`). These were untracked before the audit and
  were deliberately left untracked.
- **Remote status:** `origin/feat/arabic-edition` is in sync (pushed
  2026-09-22, regular push, no force). **Upstream was never pushed to.**

---

## 3. Completed Arabic Edition features (all present, do not regress)

- **Arabic-first:** `general.default_language` defaults to `"ar"`
  (`tui/config.py`, `config.yaml.sample`). Precedence unchanged:
  `--lang CODE` > `general.default_language` > first provider language.
- **All-providers default:** `preferred_backend` defaults to
  `all-providers` (parallel fan-out in `tui/search.py`); `auto()` stays
  sequential-first-hit for users who want it.
- **English fallback:** `fallback_language: "en"` (empty disables),
  `auto_fallback_download: false`. Fallback runs only when the target
  set is empty; results are marked, never silently downloaded unless
  opted in. TUI + headless share `search_with_fallback` (parity).
- **Format preservation:** `Candidate.format` carried end to end;
  staging sniffs content (`[V4+ Styles]`→ass, `[V4 Styles]`→ssa, else
  srt); legacy SubDL/SubSource archive paths preserve ass/ssa/vtt/sub
  member suffixes (fixed 2026-09-22, see §5). Legacy OpenSubtitles
  `process_media_file` intentionally still writes SRT (commented in
  code — legacy limitation, see §7).
- **Alternate names / Arabic normalization:** `get_alternate_names` is
  the single query-building choke point (movies get title+year
  variants; episodes get SxxEyy/1x03/E03 shapes); Arabic folding
  (harakat/tatweel/alef/teh-marbuta/alef-maksura/Arabic-Indic digits)
  applies to **matching only**; definite-article stripping and
  per-script variants are additive.
- **Fit / Compatibility ranking:** results sort by `compatibility`
  percent (see §4), not legacy `score`. UI shows compact Fit %,
  Candidate Preview shows evidence/conflicts.
- **Evidence/conflicts:** `Compatibility.evidence_lines()` renders the
  Conflict block *instead of* evidence when identity facets contradict;
  group differences are a small penalty, never an identity conflict.
- **Edition/source/resolution/group:** Director's Cut / Theatrical /
  Extended / Unrated / Remastered / Criterion / IMAX detected with
  release-context guard (`dc`/`imax` need a nearby year/resolution/
  source/codec token — `DC League of Super-Pets` is safe); source
  matching is word-boundary aware (`Charlotte Web` is safe); year
  prefers release-context occurrence (film `2012` survives).
- **Film-vs-episode:** a film never matches a subtitle claiming an
  episode and vice versa (capped MISMATCH).
- **Windows launcher `ARABIC_SUBS.bat`:** drag-and-drop (one or many
  files via `%*`), folders, Arabic paths, spaces, parentheses-safe
  (`goto` branches, no `if` blocks), ASCII-only file, anchors cwd to
  the launcher dir, native Windows file picker when launched without
  args (PowerShell `OpenFileDialog`, stays in-process so Arabic names
  survive), always `--lang ar --backend all-providers`, manual
  selection only (passes no auto-download flag), pauses on failure so
  errors are visible. Hardcoded interpreter: `C:\Python314\python.exe`
  (line 15) — change it if the user's Python lives elsewhere.
- **Send To:** per `Readme.md` §"Windows Send To": copy
  `ARABIC_SUBS.bat` (or a shortcut) into `shell:sendto`; right-click →
  Send To works like a drop. Covered by `tui/tests/test_launcher.py`.

---

## 4. Compatibility/Fit design (`library/compatibility.py`)

- **What Fit means:** verified compatibility evidence against the media
  file, NOT a statistical probability.
- **0–100 behavior:** facet points summed over a fixed denominator;
  unknown facets earn nothing and are not mismatches (only `edition`
  and `title` absences are reported). Points:
  title 45, year 10, season 6, episode 20, edition 7, source 5,
  resolution 3, group 2, codec 2. Partial title = 75% of title points.
- **Hash priority:** `hash_match=True` short-circuits to 100/BEST
  ("exact file hash"). Without a hash, filename-only results are capped
  at 90 (`FILENAME_ONLY_CEILING`).
- **Facets:** title/year/season/episode/edition/source/resolution/group/
  codec contribute per the points above; group mismatch is a small
  penalty + "different group" line, never a Conflict block.
- **Unknown metadata:** earns nothing, reported only for edition/title;
  never treated as a positive match and never as a conflict.
- **Known conflicts:** title/season/episode/edition contradictions emit
  `Conflict:` lines and cap the score: title 20, season 25, episode 25,
  film-vs-episode 25, edition 34.
- **Provider reliability:** tiebreak among equally-scored candidates
  only (derived from `AUTO_PRIORITY`: SubSource 3, OpenSubtitles 2,
  SubDL 1).
- **Excluded from Fit:** download count, provider reliability,
  HI/AI flags (visible as flags, never points).
- **Badge ranges** (`compatibility_badge`, verified 2026-09-22):
  ≥90 BEST, ≥75 GREAT, ≥55 GOOD, ≥35 MAYBE, else MISMATCH.
- **Single-source rule (added 2026-09-22):** the compound-token collapse
  table and the release-group rule live in `library/subtitle_utils.py`
  (`collapse_release_technical_tokens`, `release_group_of`) and are
  imported by `compatibility.py`, so the ranker and the provider
  queries share one reading of a release name.

---

## 5. Audit results (full audit completed 2026-09-22, Phases 0–6)

**Baseline before fixes:** `python -m pytest tui/tests -q` → **501
passed** (~60 s); `python -m compileall -q library tui
download_subs.py` → clean; `ruff check` → 44 errors (7 pre-existing on
`upstream/master`, 37 fork-introduced); `black --check` → 32 files
(15 pre-existing upstream — black was never a gate, left alone).

**Verification layer audited, no bugs found:** `SubtitleVerifier`
(format/language/coverage checks, lenient where it must be: unknown
formats pass, no ffprobe → pass, non-ar/en languages pass; strict where
it counts: empty/unreadable/wrong-script/<50% coverage/>300 s overrun),
staging containment + atomic `os.replace` + conflict pre/post checks in
`tui/jobs.py`, terminating retry loops in `tui/app.py` and
`tui/headless.py` (attempted-set grows monotonically over a finite
list; global auth/quota failures and conflicts never trigger the walk).
Spot-checked prior fixes H1/H4/M1/M3/M4/M5 — all correct in code.

**5 proven bugs found and fixed (TDD: RED test → fix → GREEN, one
commit each):**

1. **SubDL absolute-URL ID extraction** (`ccabe28`)
   - Symptom: `extract_subdl_subtitle_id("https://dl.subdl.com/subtitle/…")`
     returned `"dl.subdl.com"`, corrupting every SubDL candidate id and
     collapsing the id-keyed dedupe in `_gather_candidates` to one row.
   - Root cause: `parts[2]` indexing assumed relative URLs, while
     `_download_url` already accepts absolute ones.
   - Fix: take the path segment after `'subtitle'`. Relative-URL
     behavior unchanged.
   - Tests: `test_extract_subdl_subtitle_id_accepts_relative_and_absolute_urls`,
     `test_extract_subdl_subtitle_id_rejects_empty_urls`.
2. **REMUX treated as title** (`63f0542`)
   - Symptom: same-film REMUX pair scored 76 (partial title) not 90.
   - Root cause: post-M1, `_title_words` consumes only the *matched*
     source token, so `remux` survived whenever BluRay won `_source_of`.
   - Fix: `remux` added to `NOISE_WORDS` (5-letter distinctive token;
     source facet still reads the underlying BluRay). Measured 76→90.
   - Test: `test_a_remux_tag_is_not_part_of_the_title` (incl. "the REMUX
     tag changes nothing" percent-equality assertion).
3. **Season-less episodes** (`315d911`)
   - Symptom: `Show.E03.2020…` → `TypeError` swallowed → **zero**
     provider queries; without a year → literal garbage query
     `show-episode-None-3`.
   - Root cause: `_episode_formats` formatted a `None` season with
     `:02d` and appended an unconditional `-episode-{season}-` slug.
   - Fix: season-less E-shapes, guarded year block, episode-only slug.
     Season-known output byte-identical (verified).
   - Tests: `test_get_alternate_names_handles_an_episode_without_a_season`,
     `test_get_alternate_names_handles_a_season_less_episode_with_a_year`.
4. **Movie query junk tokens** (`85038b3`)
   - Symptom: `Dune…DDP5.1-GRP` produced provider queries
     `dune part two ddp5 1 grp`, contradicting the documented
     "technical tokens never reach the provider" contract.
   - Root cause: the H2 audio-collapse lived only in
     `compatibility.py`; `_title_hypotheses` never applied it and never
     stripped release groups (movies have no episode boundary to cut
     the tail).
   - Fix: moved collapse table + group rule to `subtitle_utils.py` as
     the single source of truth; `_title_hypotheses` applies both
     before splitting. Full suite green with **zero** pinned-score
     changes (legacy scorer cleaning is symmetric).
   - Tests: `test_get_alternate_names_drops_audio_lines_and_group_from_movie_queries`,
     `test_title_hypotheses_keep_dash_separated_title_words`.
5. **`.sub` preservation in legacy archive paths** (`563c77c`)
   - Symptom: SubDL ignored `.sub` pack members; SubSource rewrote a
     matched `.sub` member with a `.srt` extension.
   - Root cause: narrow case-sensitive member scan, ext allowlists
     missing `sub`.
   - Fix: `.sub` in both scans (case-insensitive for SubDL), member
     suffix (lowercased) through on write; corrected the stale
     `_download_archive` docstring that still described pre-H5
     extract-everything behavior.
   - Tests: `test_subdl_zip_preserves_a_sub_format_member` (uppercase
     `.SUB` member selected by episode, written lowercase `.sub`),
     `test_subsource_archive_preserves_a_sub_format_member`.

---

## 6. Final verification (fresh, 2026-09-22 — do not invent, do not re-run blindly)

- Full suite: `python -m pytest tui/tests -q` → **510 passed**
  (501 baseline + 9 new regression tests), ~59 s.
- Focused suites run green at each step (`test_providers.py`,
  `test_compatibility.py`, plus `-k` selections per fix).
- `python -m compileall -q library tui download_subs.py` → clean
  (exit 0, no output).
- `python -m ruff check library tui download_subs.py` → **all checks
  passed** (44 → 0, including the 7 pre-existing upstream violations).
- `black --check` → 29 files would reformat (was 32; upstream was
  never black-clean — advisory only, intentionally not reformatted to
  avoid diff noise).
- No other gates exist (no mypy/pytest-cov config; `pyproject.toml`
  testpaths = `tui/tests`; dev group: black, pytest, ruff).
- Offline end-to-end simulations (no network): Arabic variants emitted;
  Director's Cut vs Theatrical → 34 MISMATCH with Conflict block;
  same-episode → 83 GREAT; wrong episode → 25 MISMATCH; season-pack
  identity `(2, 3)`; `test_launcher.py` (bat structural tests) green
  inside the 510.

---

## 7. Known limitations / intentionally deferred (do NOT "fix" without a plan)

- **Absolute episode numbering** (`Show.103.mkv` → treated as film):
  investigated; **do not implement** — `Show.103` (episode) vs
  `Flight.93` (film) is indistinguishable by name alone; needs TMDB,
  which is out of scope. The unambiguous `Show.E03` form is handled
  (§5.3).
- **M12 — library prints to stdout under the TUI** (~100
  `console.print`/`rprint` calls in `library/`): real but
  **architectural** (needs an injected emitter seam across every
  client). Requires maintainer approval before implementing.
- **M13 — legacy `score`/`match_reasons` computed but rendered
  nowhere:** needs a **product decision** (surface in detail pane vs
  delete + remove ~6 pinned tests). Deferred deliberately.
- **L1/L2 — no retry/backoff, no result cache:** larger design changes;
  follow-up work, not started.
- **L3** provider HI flag plumbing, **L4** `token.pkl` pickle in source
  tree (local-tamper prerequisite; low), **L6** 7z-listing fragility
  (fallback path only), **L11** REMUX-vs-BluRay invisible as a facet
  (by design — a remux *is* BluRay-sourced; not a conflict).
- **L8** stale `1_download_subs.bat` (hardcoded
  `D:\PycharmProjects\…` path): pre-existing upstream cruft,
  README-disclaimed; left alone (deleting user-visible files unasked
  is riskier).
- **L9** config-tab language is session-only: by design (saved diff
  shows `session.language`); no separate `default_language` control
  exists in the Config view.
- **L12 cross-provider duplicates:** do not auto-merge — rows carry
  different download refs/trust and the UI exposes provider choice;
  compatibility sort already co-locates equivalents.
- **Legacy OpenSubtitles always writes SRT:** documented in code at
  `process_media_file`; full legacy consolidation (old M11) deferred.
- **ASS centisecond parsing** in the verifier (`0:05:12.50` reads
  312.05 not 312.5): ≤0.5 s error, can never flip 50%-coverage or
  300 s-overrun decisions; cosmetic, untouched.
- **Pre-existing style:** black was never green (even upstream); ruff
  is now green — keep it green.
- **Deferred Phase-2 features:** TMDB metadata, AI translation, RTL UI,
  FPS/runtime compatibility (needs `ffprobe` facet design).

---

## 8. Security notes

- **Never commit** `config.yaml`, `library/token.pkl`, credentials, or
  API keys. All three session files confirm: gitignored and absent
  from history (`git ls-files` shows none; `git log --all --
  config.yaml` empty).
- API keys live only in the local untracked `config.yaml` (see
  `config.yaml.sample` for the shape). Provider errors pass through
  `redact_secrets`; `public_url` returns absolute, key-free URLs or
  `None` (SubDL keyed URLs correctly yield `None`).
- Audit findings 2026-09-22: no `shell=True`, no `extractall`/ZipSlip
  (archives are read-then-written, never extracted by member path),
  no `eval`; subprocess calls are argv-form with timeouts
  (ffprobe 10 s, 7z via list-form); TUI staging confines writes to a
  `TemporaryDirectory` with a containment check and atomic
  `os.replace`; no command injection via filenames (single argv
  elements); `.bat` `%*`/PowerShell-variable forwarding is quoting-safe.
- `ARABIC_SUBS.bat` is ASCII-only by requirement (code-page safety);
  Arabic paths travel as arguments, never as file content.

---

## 9. How to run (exact, verified against Readme.md)

```powershell
# Full test suite (the regression gate — must stay green before committing)
python -m pytest tui/tests -q
# Focused compatibility tests
python -m pytest tui/tests/test_compatibility.py -q
# Syntax gate
python -m compileall -q library tui download_subs.py
# Lint gate (must stay green — was 44 errors, now 0)
python -m ruff check library tui download_subs.py

# Normal TUI, single file (Arabic is the default language)
python download_subs.py "path/to/movie.mkv"
# Multiple inputs / folder / recursive / custom output dir
python download_subs.py "path/to/movie.mkv" "path/to/show/season 01"
python download_subs.py --recursive "path/to/movies"
python download_subs.py --output-dir "path/to/subtitles" "path/to/movies"
# Explicit language/engine overrides
python download_subs.py --lang en --backend subdl "path/to/movie.mkv"
python download_subs.py --backend all-providers "movie.mkv"
# Headless (no TUI) and forced TUI
python download_subs.py --no-tui --lang ar --recursive "D:\Shows"
python download_subs.py --tui "path/to/movie.mkv"
python download_subs.py --help
```

- **Arabic launcher:** double-click `ARABIC_SUBS.bat` (file picker) or
  drag-and-drop files/folders onto it (multi-file supported). Requires
  `C:\Python314\python.exe` (line 15) — adjust if Python lives elsewhere.
- **Send To:** copy `ARABIC_SUBS.bat` (or a shortcut to it) into the
  folder opened by `shell:sendto`; then right-click media → Send To →
  launcher.

---

## 10. How future AI sessions should start (mandatory checklist)

1. Read this file (`PROJECT_STATE.md`) first.
2. Read `AGENTS.md` / `CLAUDE.md` if present (note: `AGENTS.md` routes
   ranking/scoring/parsing/architecture work to an `architect`
   subagent — if that route is unavailable, do the work directly and
   carefully instead).
3. Read `docs/superpowers/specs/2026-09-16-arabic-edition-design.md`
   before touching language defaults, fallback flow, ranking, or
   format preservation.
4. Inspect `git status`, `git log --oneline --decorate -10`,
   `git remote -v`. Confirm branch is `feat/arabic-edition`,
   `origin` = fork, `upstream` = ach-raf. **Never push to upstream.**
5. Do NOT repeat completed audit phases (§5) or re-fix the 5 bugs in
   §5 — they have regression tests; re-verify via the suite instead.
6. Do NOT modify working behavior without a reproduced bug (failing
   test or interpreter transcript proving symptom → root cause).
7. Use TDD for fixes: RED test first, minimal fix, GREEN, neighboring
   focused tests, full suite, one logical commit per bug.
8. Run verification-before-completion before claiming success: full
   suite + compileall + ruff from a clean tree, fresh evidence only.
9. Never commit `config.yaml`, `library/token.pkl`, credentials, or
   generated junk; keep unrelated untracked files untracked.

---

## 11. Current next steps (genuinely useful only; optional marked)

- **Manual network tests** (need real providers/files; listed but never
  run — no network in audit): health probe (`r` shows latency),
  season-pack single-episode extraction, cp1256/UTF-16 Arabic cleaning,
  `;`-separator ads cleaning, SubSource "copy public URL", `Charlotte
  Web`-style title guard. Optional.
- **M12 stdout emitter** (architectural; needs approval). Optional.
- **M13 product call** (surface vs delete `score`/`match_reasons`).
  Optional, needs decision first.
- **L1/L2 retries + search cache** (design + benchmarks). Optional.
- Everything else in §7 is *not* recommended work — read the reasons
  there before touching it.

---

## 12. Session handoff summary — where we are now

The Arabic Edition is complete and audited on `feat/arabic-edition`,
pushed to `origin` (upstream untouched). Baseline was 501 passing tests;
a full audit verified the verification layer and prior fixes as correct,
then found and fixed 5 real bugs with TDD (SubDL absolute-URL ids, REMUX
title residue 76→90, season-less episode queries, movie query junk,
`.sub` preservation), each in its own commit, plus a lint commit taking
ruff from 44 errors to zero. Final state: **510 tests pass**,
compileall and ruff clean, working tree clean, remotes verified. Known
limitations (M12/M13/L-items, absolute numbering, legacy SRT path) were
each investigated and deliberately left alone with reasons in §7 —
do not re-litigate them without new evidence. Next developer: follow
the §10 checklist, keep the suite green, and pick only from §11 —
starting with the manual network tests, since everything verifiable
offline is already verified.
