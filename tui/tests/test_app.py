import asyncio
import html
import re
import subprocess
import sys
from pathlib import Path

import pytest
from textual.color import Color
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Static

from tui.app import (
    CandidatePreview,
    ConfirmConfigExit,
    ConfirmConfigSave,
    ConfirmQuit,
    SubsApp,
)
from tui.config import ConfigRepository
from tui.domain import Candidate, EngineMode, Provider, ProviderSearchResult, QueueStatus
from tui.search import CoordinatedSearchResult, SearchCoordinator
from tui.widgets.overlays.engine_switcher import EngineSwitcher
from tui.widgets.overlays.lang_popover import LanguagePopover
from tui.widgets.results_table import ResultsTable
from tui.widgets.views import ConfigView


class FakeCoordinator:
    def __init__(self, candidates):
        self.candidates = candidates
        self.requests = []
        self.auto_requests = []
        self.all_providers_requests = []
        self.fallback_calls = []

    def concrete(self, provider, request):
        self.requests.append(request)
        return CoordinatedSearchResult(
            candidates=list(self.candidates),
            attempted=[provider],
            selected_provider=provider,
        )

    def auto(self, request, health=None):
        self.requests.append(request)
        self.auto_requests.append(request)
        return CoordinatedSearchResult(candidates=list(self.candidates))

    def all_providers(self, request, health=None):
        self.requests.append(request)
        self.all_providers_requests.append(request)
        return CoordinatedSearchResult(candidates=list(self.candidates))

    def search_with_fallback(
        self,
        request,
        run_search,
        fallback_language="",
        auto_download=False,
    ):
        """Run the injected mode entry point, then hand back whatever it returned.

        The two-language orchestration belongs to and is tested against
        SearchCoordinator.search_with_fallback.
        """
        self.fallback_calls.append(
            (request.language, fallback_language, auto_download)
        )
        return run_search(request)


def test_recursive_startup_adds_nested_media_to_queue(tmp_path):
    nested_media = tmp_path / "Movie" / "movie.mkv"
    nested_media.parent.mkdir()
    nested_media.touch()

    app = SubsApp(
        config={},
        media_paths=[str(tmp_path)],
        overrides={},
        recursive_search=True,
    )

    assert [item.path for item in app.state.queue] == [nested_media.resolve()]


def test_startup_uses_configured_media_extension_overrides(tmp_path):
    included = tmp_path / "Movie.custom"
    excluded = tmp_path / "Ignored.mkv"
    included.touch()
    excluded.touch()

    app = SubsApp(
        config={
            "general": {
                "media_extensions": {
                    "include": [".CUSTOM"],
                    "exclude": ["mkv"],
                }
            }
        },
        media_paths=[str(tmp_path)],
        overrides={},
    )

    assert [item.path for item in app.state.queue] == [included.resolve()]


@pytest.fixture
def configured_app(tmp_path):
    media = tmp_path / "الهيبة.S01E03.mkv"
    media.touch()
    # The coordinator is fake, so the compatibility fields the real search
    # pipeline would derive are set here, the same way ``score`` always was.
    candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id="1",
            release="الهيبة - S01E03 WEB-DL",
            language="ar",
            download_count=2400,
            score=94,
            compatibility=94,
            compatibility_badge="BEST",
            compatibility_evidence=(
                "Evidence:",
                "+ exact title",
                "+ S01E03",
                "- no hash match",
            ),
        ),
        Candidate(
            provider=Provider.SUBDL,
            provider_id="2",
            release="Al Hayba S01E03",
            language="ar",
            score=81,
            compatibility=81,
            compatibility_badge="GREAT",
            compatibility_evidence=(
                "Evidence:",
                "+ partial title",
                "+ S01E03",
                "- no hash match",
            ),
        ),
    ]
    coordinator = FakeCoordinator(candidates)
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": True,
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"Arabic": "ar", "English": "en"},
            },
        },
        media_paths=[str(media)],
        overrides={},
        coordinator=coordinator,
    )
    return app, coordinator


def test_all_four_tabs_are_real_views(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for key, view in (
                ("f1", "search"),
                ("f2", "queue"),
                ("f3", "history"),
                ("f4", "config"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert app.state.active_view == view
                assert (
                    app.query_one("#workspace", ContentSwitcher).current
                    == f"{view}-view"
                )

    asyncio.run(run())


def test_view_shortcuts_focus_each_primary_workspace(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            for key, selector in (
                ("f2", "#queue-table"),
                ("f3", "#history-table"),
                ("f4", "#config-engine"),
                ("f1", "#results-table"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert app.focused is app.query_one(selector)

    asyncio.run(run())


def test_escape_returns_query_focus_to_results(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            await pilot.press("slash")
            assert isinstance(app.focused, Input)

            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is app.query_one(ResultsTable)

    asyncio.run(run())


def test_all_providers_choice_updates_engine_label_and_search_mode(configured_app):
    app, coordinator = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            app._engine_chosen(EngineMode.ALL_PROVIDERS)
            await pilot.pause()

            assert app.state.engine_mode is EngineMode.ALL_PROVIDERS
            assert app.all_providers_mode is True
            assert "ENGINE All providers" in str(
                app.query_one("#chip-engine", Button).label
            )
            assert coordinator.all_providers_requests

    asyncio.run(run())


def test_all_providers_mode_dispatches_all_providers_search(configured_app):
    app, coordinator = configured_app
    app.state.choose_engine(EngineMode.ALL_PROVIDERS)
    app.set_reactive(SubsApp.all_providers_mode, True)

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert coordinator.all_providers_requests
            assert not coordinator.auto_requests

    asyncio.run(run())


def test_all_providers_auto_selection_downloads_first_ranked_candidate(
    configured_app,
):
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id="best",
            release="best",
            language="ar",
            score=99,
        ),
        Candidate(
            provider=Provider.OPENSUBTITLES,
            provider_id="lower",
            release="lower",
            language="ar",
            score=70,
        ),
    ]
    app.state.choose_engine(EngineMode.ALL_PROVIDERS)
    app.application_config.general.preferred_backend = EngineMode.ALL_PROVIDERS
    app.application_config.general.auto_selection = True
    app.set_reactive(SubsApp.all_providers_mode, True)
    downloaded = []
    app.run_download = (
        lambda _item_key, candidate, _overwrite: downloaded.append(candidate)
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert [candidate.provider_id for candidate in downloaded] == ["best"]

    asyncio.run(run())


def test_all_providers_raw_dictionary_config_uses_canonical_mode():
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "all-providers",
            },
            "subdl": {"api_key": "configured"},
        },
        media_paths=[],
        overrides={},
    )

    assert app.state.engine_mode is EngineMode.ALL_PROVIDERS
    assert app.application_config.general.preferred_backend is EngineMode.ALL_PROVIDERS
    assert app.all_providers_mode is True


def test_all_providers_toggle_restores_previous_mode_and_falls_back_to_auto():
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": True,
            },
            "subdl": {"api_key": "configured"},
        },
        media_paths=[],
        overrides={},
    )

    async def run():
        async with app.run_test():
            app.action_toggle_all_providers()
            assert app.state.engine_mode is EngineMode.ALL_PROVIDERS
            app.action_toggle_all_providers()
            assert app.state.engine_mode is EngineMode.SUBDL

        # ASK is stated explicitly rather than left to the configured default: it is
        # the mode the toggle refuses to restore, so the app must start there for the
        # AUTO fallback below to be reached.
        fallback_app = SubsApp(
            config={"general": {"preferred_backend": "ask"}},
            media_paths=[],
            overrides={},
        )
        async with fallback_app.run_test():
            fallback_app.action_toggle_all_providers()
            assert fallback_app.state.engine_mode is EngineMode.ALL_PROVIDERS
            fallback_app.action_toggle_all_providers()
            assert fallback_app.state.engine_mode is EngineMode.AUTO

    asyncio.run(run())


def test_engine_choice_outside_config_view_persists_canonical_mode(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "general:\n"
        "  preferred_backend: subdl\n"
        "  default_language: ar\n"
        "  skip_interactive_menu: true\n"
        "subdl:\n"
        "  api_key: configured\n"
        "  languages:\n"
        "    Arabic: ar\n",
        encoding="utf-8",
    )
    media = tmp_path / "movie.mkv"
    media.touch()
    coordinator = FakeCoordinator([])
    app = SubsApp(
        media_paths=[str(media)],
        config_path=str(config_path),
        coordinator=coordinator,
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert app.state.active_view == "search"
            app._engine_chosen(EngineMode.ALL_PROVIDERS)
            await pilot.pause()

    asyncio.run(run())

    reloaded = ConfigRepository(config_path).load()
    assert reloaded.general.preferred_backend is EngineMode.ALL_PROVIDERS


def test_engine_choice_does_not_persist_cli_only_language_override(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "general:\n"
        "  preferred_backend: subdl\n"
        "  default_language: en\n"
        "  skip_interactive_menu: false\n"
        "subdl:\n"
        "  api_key: configured\n"
        "  languages:\n"
        "    English: en\n",
        encoding="utf-8",
    )
    media = tmp_path / "movie.mkv"
    media.touch()
    app = SubsApp(
        media_paths=[str(media)],
        config_path=str(config_path),
        coordinator=FakeCoordinator([]),
        overrides={"lang": "ar"},
        language_resolution=("ar", "cli"),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert app.application_config.general.skip_interactive_menu is True
            app._engine_chosen(EngineMode.ALL_PROVIDERS)
            await pilot.pause()

    asyncio.run(run())

    reloaded = ConfigRepository(config_path).load()
    assert reloaded.general.preferred_backend is EngineMode.ALL_PROVIDERS
    assert reloaded.general.skip_interactive_menu is False


def test_toggle_all_providers_outside_config_view_persists_canonical_mode(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "general:\n"
        "  preferred_backend: subdl\n"
        "  default_language: ar\n"
        "  skip_interactive_menu: true\n"
        "subdl:\n"
        "  api_key: configured\n"
        "  languages:\n"
        "    Arabic: ar\n",
        encoding="utf-8",
    )
    media = tmp_path / "movie.mkv"
    media.touch()
    app = SubsApp(
        media_paths=[str(media)],
        config_path=str(config_path),
        coordinator=FakeCoordinator([]),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert app.state.active_view == "search"
            generation = app.search_generation
            app.action_toggle_all_providers()
            await pilot.pause()
            assert app.search_generation > generation
            assert app.coordinator.all_providers_requests

    asyncio.run(run())

    reloaded = ConfigRepository(config_path).load()
    assert reloaded.general.preferred_backend is EngineMode.ALL_PROVIDERS


def test_palette_engine_action_persists_selected_mode(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "general:\n"
        "  preferred_backend: all-providers\n"
        "  default_language: ar\n"
        "  skip_interactive_menu: true\n"
        "subdl:\n"
        "  api_key: configured\n"
        "  languages:\n"
        "    Arabic: ar\n",
        encoding="utf-8",
    )
    media = tmp_path / "movie.mkv"
    media.touch()
    app = SubsApp(
        media_paths=[str(media)],
        config_path=str(config_path),
        coordinator=FakeCoordinator([]),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._set_engine_action(EngineMode.SUBDL)
            await pilot.pause()

    asyncio.run(run())

    reloaded = ConfigRepository(config_path).load()
    assert reloaded.general.preferred_backend is EngineMode.SUBDL


def test_repeated_view_switching_does_not_rebuild_unchanged_tables(
    configured_app,
):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            tables = [
                app.query_one(ResultsTable),
                app.query_one("#queue-table", DataTable),
                app.query_one("#history-table", DataTable),
            ]
            clear_counts = [0, 0, 0]
            for index, table in enumerate(tables):
                original_clear = table.clear

                def tracked_clear(
                    *args,
                    _index=index,
                    _clear=original_clear,
                    **kwargs,
                ):
                    clear_counts[_index] += 1
                    return _clear(*args, **kwargs)

                table.clear = tracked_clear

            for _ in range(8):
                await pilot.press(
                    "f2",
                    "down",
                    "f3",
                    "f1",
                    "down",
                    "up",
                )
            await pilot.pause(0.1)

            assert clear_counts == [0, 0, 0]
            assert app.focused is app.query_one(ResultsTable)

    asyncio.run(run())


def test_obvious_engine_and_language_shortcuts_open_choices(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, EngineSwitcher)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LanguagePopover)

    asyncio.run(run())


def test_interactive_startup_opens_engine_before_refreshing_workspace(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()

    class StartupOrderApp(SubsApp):
        CSS_PATH = str(Path(__file__).parents[1] / "style.tcss")

        def __init__(self, *args, **kwargs):
            self.startup_events = []
            super().__init__(*args, **kwargs)

        def action_open_engine(self):
            self.startup_events.append("engine")
            super().action_open_engine()

        def _refresh_all(self):
            self.startup_events.append("workspace")
            super()._refresh_all()

    app = StartupOrderApp(
        config={"general": {"preferred_backend": "ask"}},
        media_paths=[str(media)],
        overrides={},
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert isinstance(app.screen, EngineSwitcher)
            assert app.startup_events[:2] == ["engine", "workspace"]

    asyncio.run(run())


def test_initial_folder_language_choice_applies_to_every_file_without_scope_prompt(
    tmp_path,
):
    media_paths = []
    for name in ("episode-01.mkv", "episode-02.mkv"):
        media = tmp_path / name
        media.touch()
        media_paths.append(str(media))
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": False,
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"English": "en", "Arabic": "ar"},
            },
        },
        media_paths=media_paths,
        overrides={},
        coordinator=FakeCoordinator([]),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LanguagePopover)

            await pilot.press("down", "enter")
            await pilot.pause()

            assert not isinstance(app.screen, LanguagePopover)
            assert [item.language for item in app.state.queue] == ["ar", "ar"]

    asyncio.run(run())


def test_multi_file_tui_uses_resolved_language_without_startup_picker(tmp_path):
    media_paths = []
    for name in ("episode-01.mkv", "episode-02.mkv"):
        path = tmp_path / name
        path.touch()
        media_paths.append(str(path))
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": False,
                "default_language": "fr",
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"English": "en", "French": "fr"},
            },
        },
        media_paths=media_paths,
        overrides={},
        coordinator=FakeCoordinator([]),
        language_resolution=("fr", "config"),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, LanguagePopover)
            assert [item.language for item in app.state.queue] == ["fr", "fr"]

    asyncio.run(run())


def test_single_file_tui_prompts_to_confirm_resolved_language(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": False,
                "default_language": "fr",
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"English": "en", "French": "fr"},
            },
        },
        media_paths=[str(media)],
        overrides={},
        coordinator=FakeCoordinator([]),
        language_resolution=("fr", "config"),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LanguagePopover)
            assert app.screen.current == "fr"

    asyncio.run(run())


def test_whitespace_cli_language_preserves_single_file_config_confirmation(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": False,
                "default_language": "fr",
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"English": "en", "French": "fr"},
            },
        },
        media_paths=[str(media)],
        overrides={"lang": "   ", "backend": None},
        coordinator=FakeCoordinator([]),
        language_resolution=("fr", "config"),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LanguagePopover)
            assert app.screen.current == "fr"
            assert app.state.language == "fr"

    asyncio.run(run())


def test_single_file_tui_uses_cli_resolved_language_without_startup_picker(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": False,
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"English": "en", "Arabic": "ar"},
            },
        },
        media_paths=[str(media)],
        overrides={},
        coordinator=FakeCoordinator([]),
        language_resolution=("ar", "cli"),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, LanguagePopover)
            assert [item.language for item in app.state.queue] == ["ar"]

    asyncio.run(run())


def test_multi_file_tui_rejects_missing_language_before_mount(tmp_path):
    media_paths = []
    for name in ("episode-01.mkv", "episode-02.mkv"):
        path = tmp_path / name
        path.touch()
        media_paths.append(str(path))

    with pytest.raises(
        ValueError,
        match=r"No language selected\. Use --lang or set general\.default_language\.",
    ):
        SubsApp(
            config={
                "general": {
                    "preferred_backend": "subdl",
                    "skip_interactive_menu": False,
                },
                "subdl": {"api_key": "configured", "languages": {}},
            },
            media_paths=media_paths,
            overrides={},
            coordinator=FakeCoordinator([]),
            language_resolution=("", "missing"),
        )


def test_tui_batch_missing_language_exits_cleanly_in_subprocess(tmp_path):
    first = tmp_path / "episode-01.mkv"
    second = tmp_path / "episode-02.mkv"
    first.touch()
    second.touch()
    script = (
        "from tui.app import run_tui; "
        f"run_tui(config={{'general': {{'preferred_backend': 'subdl'}}, "
        f"'subdl': {{'api_key': 'configured', 'languages': {{}}}}}}, "
        f"media_paths={[str(first), str(second)]!r}, overrides={{}}, "
        f"config_path={str(tmp_path / 'missing-config.yaml')!r}, "
        "language_resolution=('', 'missing'))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "No language selected. Use --lang or set general.default_language." in output
    assert "Traceback" not in output


def test_query_submit_uses_edited_value(configured_app):
    app, coordinator = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("slash")
            query = app.query_one("#query-input", Input)
            query.value = "Director Cut"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app.last_search_request.query == "Director Cut"
            assert coordinator.requests[-1].query == "Director Cut"

    asyncio.run(run())


def test_results_and_multilingual_detail_are_visible(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert app.query_one(ResultsTable).row_count == 2
            assert "الهيبة" in app.query_one("#detail-title", Static).content
            detail = str(app.query_one("#detail-kv", Static).content)
            assert "Compatibility: 94% BEST" in detail
            selected = app.current_candidate()
            for line in selected.compatibility_evidence:
                assert line in detail
            hash_line = next(
                line for line in detail.splitlines() if "[dim]Hash match[/dim]" in line
            )
            assert hash_line.endswith("no")
            # The detail block is Uploader, Downloads, the Compatibility headline,
            # the evidence lines the engine emitted, Hash match and Flags: four
            # fixed newlines plus one per evidence line. The engine emits at most
            # eleven (a header, a title signal, one per facet, a hash signal), so
            # fifteen is the ceiling -- a real bound, not a hand-tuned number, and
            # it still catches a renderer that grows without bound.
            assert detail.count("\n") <= 15
            assert str(app.query_one("#download-selected", Button).label) == "Get  ↵"
            assert str(app.query_one("#preview-selected", Button).label) == "View  p"
            assert str(app.query_one("#copy-url", Button).label) == "URL  y"

    asyncio.run(run())


def test_command_deck_chrome_matches_compact_visual_contract(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            assert app.query_one("TopBar").region.height == 3
            assert app.query_one("QueryBar").region.height == 3
            assert "ENGINE" in str(app.query_one("#chip-engine", Button).label)
            assert "LANG" in str(app.query_one("#chip-language", Button).label)
            assert app.query_one("#chip-command", Button).label
            assert app.query_one("#chip-health", Static).content

            status = app.query_one("#status-settings", Static).content
            assert "engine" in status
            assert "lang" in status
            assert "utf-8" in status
            assert "clean" in status
            assert "sync" in status
            assert "HI" in status

    asyncio.run(run())


def test_search_workbench_exposes_mockup_panel_content(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            results_panel = app.query_one("#results-panel")
            detail_panel = app.query_one("#detail-panel")
            assert results_panel.region.width >= detail_panel.region.width * 2
            assert 30 <= detail_panel.region.width <= 42
            assert "RESULTS" in app.query_one("#results-heading", Static).content
            assert "sorted by fit" in app.query_one(
                "#results-heading", Static
            ).content.lower()
            assert "PREVIEW" in app.query_one("#preview-heading", Static).content
            assert app.query_one("#preview-selected", Button)
            assert app.query_one("#copy-url", Button)

    asyncio.run(run())


def test_results_table_keeps_release_and_fit_percentage_visible(configured_app):
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id="long-release",
            release=(
                "Inception.2010.2160p.UHD.BluRay.REMUX."
                "HDR.HEVC.TrueHD.Atmos-FullReleaseName"
            ),
            language="en",
            download_count=48213,
            score=100,
            # The widest value the column ever renders, so the width check below
            # is the real worst case rather than a comfortable sample.
            compatibility=100,
            compatibility_badge="BEST",
            compatibility_evidence=("Evidence:", "+ exact title", "- no hash match"),
        )
    ]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            table = app.query_one(ResultsTable)
            assert list(table.columns.values())[0].label.plain == "#"
            release_column = list(table.columns.values())[1]
            assert release_column.label.plain == "Release"
            assert release_column.width >= 71
            assert table.get_cell_at((0, 2)) == "EN"
            rendered_fit = table.get_cell_at((0, len(table.columns) - 1))
            fit_column = list(table.columns.values())[-1]
            assert fit_column.label.plain == "Fit"
            assert rendered_fit.plain == "100%"
            assert len(rendered_fit.plain) <= fit_column.width
            assert table.max_scroll_x == 0

    asyncio.run(run())


def test_rendered_fit_column_matches_the_order_the_rows_arrived_in(configured_app):
    # End to end through the real search coordinator: what the Fit column shows
    # must equal each candidate's compatibility, in the order they were ranked.
    # A table that re-sorted, or read the wrong field, would disagree here.
    app, _ = configured_app
    releases = [
        "Amadeus.1985.2160p.BluRay.x264-GROUP",
        "Amadeus.1984.2160p.BluRay.x264-GROUP",
        "Unrelated.1984.2160p.BluRay.x264-GROUP",
    ]

    class Adapter:
        provider = Provider.SUBDL

        def search(self, request):
            return ProviderSearchResult(
                provider=Provider.SUBDL,
                candidates=[
                    Candidate(
                        provider=Provider.SUBDL,
                        provider_id=str(index),
                        release=release,
                        language="ar",
                    )
                    for index, release in enumerate(releases)
                ],
            )

    app.coordinator = SearchCoordinator({Provider.SUBDL: Adapter()})

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("f5")
            await pilot.pause(0.3)

            table = app.query_one(ResultsTable)
            rendered = [
                table.get_cell_at((row, len(table.columns) - 1)).plain
                for row in range(table.row_count)
            ]
            expected = [f"{item.compatibility}%" for item in app.candidates]

            assert table.row_count == len(releases)
            assert rendered == expected
            assert [item.compatibility for item in app.candidates] == sorted(
                (item.compatibility for item in app.candidates), reverse=True
            )

    asyncio.run(run())


def test_results_table_selection_is_terminal_visible(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            table = app.query_one(ResultsTable)

            assert table.cursor_type == "row"
            assert table.cursor_background_priority == "css"
            assert table.zebra_stripes is True
            assert (
                table.get_component_styles("datatable--cursor").background
                == Color.parse("#2b5273")
            )

    asyncio.run(run())


def test_results_table_has_numbered_index(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            table = app.query_one(ResultsTable)

            assert list(table.columns.values())[0].label.plain == "#"
            assert table.get_cell_at((0, 0)).strip() == "1"
            assert table.get_cell_at((1, 0)).strip() == "2"

    asyncio.run(run())


def test_typing_result_number_jumps_to_row(configured_app):
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id=str(index),
            release=f"Release {index:02d}",
            language="ar",
            score=100 - index,
        )
        for index in range(1, 41)
    ]

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            table = app.query_one(ResultsTable)
            assert app.focused is table

            await pilot.press("3", "7")
            await pilot.pause()

            assert app.state.active_view == "search"
            assert table.cursor_row == 36
            assert app.cursor_index == 36

    asyncio.run(run())


def test_modified_view_shortcuts_open_each_tab(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            for key, view in (
                ("f2", "queue"),
                ("f3", "history"),
                ("f4", "config"),
                ("f1", "search"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert app.state.active_view == view

    asyncio.run(run())


def test_cycle_view_shortcuts_move_forward_and_backward(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)

            await pilot.press("ctrl+pagedown")
            await pilot.pause()
            assert app.state.active_view == "queue"

            await pilot.press("ctrl+pageup")
            await pilot.pause()
            assert app.state.active_view == "search"

    asyncio.run(run())


def test_cursor_navigation_updates_typed_selection(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert app.current_candidate().provider is Provider.SUBDL
            await pilot.press("j")
            await pilot.pause()
            assert app.cursor_index == 1
            assert app.current_candidate().provider_id == "2"

    asyncio.run(run())


def test_completed_search_focuses_results_for_arrow_navigation(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            table = app.query_one(ResultsTable)
            assert app.focused is table

            await pilot.press("down")
            await pilot.pause()
            assert app.cursor_index == 1
            assert app.current_candidate().provider_id == "2"

    asyncio.run(run())


def test_cursor_change_does_not_rebuild_results_table(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            table = app.query_one(ResultsTable)
            original_clear = table.clear
            clear_calls = 0

            def tracked_clear(*args, **kwargs):
                nonlocal clear_calls
                clear_calls += 1
                return original_clear(*args, **kwargs)

            table.clear = tracked_clear
            await pilot.press("j")
            await pilot.pause(0.1)

            assert app.cursor_index == 1
            assert clear_calls == 0

    asyncio.run(run())


def test_rapid_cursor_input_does_not_replay_stale_highlights(configured_app):
    app, coordinator = configured_app
    coordinator.candidates = [
        Candidate(
            provider=Provider.SUBDL,
            provider_id=str(index),
            release=f"Release {index:02d}",
            language="ar",
            score=100 - index,
        )
        for index in range(40)
    ]

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            table = app.query_one(ResultsTable)
            move_count = 0
            original_move = table.move_cursor

            def tracked_move(*args, **kwargs):
                nonlocal move_count
                move_count += 1
                return original_move(*args, **kwargs)

            table.move_cursor = tracked_move
            for _ in range(25):
                table.action_cursor_down()
            await pilot.pause(0.1)

            assert table.cursor_row == 25
            assert app.cursor_index == 25
            assert move_count <= 25

    asyncio.run(run())


def test_enter_downloads_highlighted_result_from_focused_table(configured_app):
    app, _ = configured_app
    selected = []
    app.run_download = lambda _item_key, candidate, _overwrite: selected.append(
        candidate.key
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            await pilot.press("down", "enter")
            await pilot.pause()

            assert selected == ["subdl:2"]

    asyncio.run(run())


def test_narrow_terminal_hides_detail_without_hiding_results(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.2)
            assert app.query_one("#detail-panel").display is False
            assert app.query_one(ResultsTable).display is True
            assert app.query_one("#chip-command").display is False
            assert app.query_one("#chip-health").display is False
            assert app.query_one("#status-settings").display is False

    asyncio.run(run())


def test_default_terminal_width_prioritizes_results_without_horizontal_scroll(
    configured_app,
):
    app, _ = configured_app

    async def run():
        async with app.run_test(size=(118, 32)) as pilot:
            await pilot.pause(0.2)
            table = app.query_one(ResultsTable)

            assert app.query_one("#detail-panel").display is False
            assert table.display is True
            assert table.max_scroll_x == 0
            assert app.query_one("#chip-command").display is False
            assert app.query_one("#chip-health").display is False
            assert app.query_one("#status-settings").display is False

    asyncio.run(run())


def test_stale_search_completion_cannot_replace_current_results(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            current = list(app.candidates)
            app._search_finished(
                app.state.active_item.key,
                app.search_generation - 1,
                app.last_search_request,
                CoordinatedSearchResult(candidates=[]),
            )
            assert app.candidates == current

    asyncio.run(run())


def test_auto_selection_uses_best_visible_candidate(configured_app):
    app, _ = configured_app
    calls = []
    app.application_config.general.auto_selection = True
    app.action_download_cursor = lambda: calls.append(app.current_candidate().key)

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert calls == ["subdl:1"]

    asyncio.run(run())


def test_provider_failure_marks_item_failed_and_advances(tmp_path):
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.touch()
    second.touch()
    fallback = Candidate(
        provider=Provider.SUBDL,
        provider_id="next",
        release="second",
        language="en",
    )

    class FailThenSucceedCoordinator(FakeCoordinator):
        def concrete(self, provider, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return CoordinatedSearchResult(
                    errors={provider: "service unavailable"},
                )
            return CoordinatedSearchResult(candidates=[fallback])

    coordinator = FailThenSucceedCoordinator([])
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": True,
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"English": "en"},
            },
        },
        media_paths=[str(first), str(second)],
        overrides={},
        coordinator=coordinator,
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.4)
            assert app.state.queue[0].status is QueueStatus.FAILED
            assert app.state.queue[0].error == ("SubDL: service unavailable")
            assert app.state.active_item.path == second
            assert app.state.active_item.status is QueueStatus.AWAITING_PICK
            assert len(coordinator.requests) >= 2

    asyncio.run(run())


def test_quit_with_unfinished_queue_requires_confirmation(configured_app):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.action_request_quit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmQuit)
            assert app.screen.query_one(Static).region.x > 0
            assert app.screen.query_one(Static).region.y > 0

    asyncio.run(run())


def test_config_draft_survives_refresh_and_blocks_accidental_navigation(
    configured_app,
):
    app, _ = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("f4")
            ads = app.query_one("#config-ads", Input)
            ads.value = "draft-ads.txt"
            original_engine = app.state.engine_mode
            original_language = app.state.language
            app._engine_chosen(EngineMode.AUTO)
            app._language_provider_scope = {Provider.SUBDL}
            app._language_chosen(("fr", "all"))
            await pilot.pause()
            assert app.query_one(ConfigView).dirty is True
            assert app.state.engine_mode is original_engine
            assert app.state.language == original_language
            assert app.config_draft.general.preferred_backend is EngineMode.AUTO
            assert app.config_draft_language == "fr"

            app.notice = "background update"
            app._refresh_all()
            assert ads.value == "draft-ads.txt"

            await pilot.press("f1")
            assert app.state.active_view == "config"
            assert isinstance(app.screen, ConfirmConfigExit)
            assert app.screen.query_one(Static).region.x > 0
            assert app.screen.query_one(Static).region.y > 0

            await pilot.press("d")
            await pilot.pause()
            assert app.query_one(ConfigView).dirty is False
            assert ads.value == ""
            assert app.state.active_view == "search"
            assert app.state.engine_mode is original_engine
            assert app.state.language == original_language
            assert app.application_config.general.preferred_backend is original_engine

    asyncio.run(run())


def test_confirmed_config_save_applies_engine_and_language_draft(
    configured_app,
):
    app, coordinator = configured_app

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("f4")
            app._engine_chosen(EngineMode.AUTO)
            app._language_provider_scope = {Provider.SUBDL}
            app._language_chosen(("fr", "all"))

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmConfigSave)
            assert app.state.engine_mode is not EngineMode.AUTO
            assert app.state.language != "fr"

            await pilot.press("enter")
            await pilot.pause()
            assert app.state.engine_mode is EngineMode.AUTO
            assert app.state.language == "fr"
            assert app.application_config.general.preferred_backend is EngineMode.AUTO
            assert (
                "fr"
                in app.application_config.providers[Provider.SUBDL].languages.values()
            )
            assert coordinator.requests[-1].language == "fr"
            assert len(coordinator.requests) >= 2

    asyncio.run(run())


def test_failed_config_write_keeps_live_state_and_dirty_draft(
    configured_app,
    tmp_path,
):
    app, _ = configured_app
    app.config_path = tmp_path / "config.yaml"

    def fail_save(_draft):
        raise PermissionError("read-only")

    app.config_repository.save = fail_save

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            original_engine = app.state.engine_mode
            await pilot.press("f4")
            app._engine_chosen(EngineMode.AUTO)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app.state.engine_mode is original_engine
            assert app.application_config.general.preferred_backend is original_engine
            assert app.query_one(ConfigView).dirty is True
            assert "Could not save configuration" in app.last_error

    asyncio.run(run())


class FallbackCoordinator(FakeCoordinator):
    """Substitutes a marked fallback result exactly as the real coordinator does."""

    def __init__(self, candidates, fallback_candidates):
        super().__init__(candidates)
        self.fallback_candidates = fallback_candidates

    def search_with_fallback(
        self,
        request,
        run_search,
        fallback_language="",
        auto_download=False,
    ):
        self.fallback_calls.append(
            (request.language, fallback_language, auto_download)
        )
        primary = run_search(request)
        if primary.candidates:
            return primary
        return CoordinatedSearchResult(
            candidates=list(self.fallback_candidates),
            used_fallback=True,
            fallback_language=fallback_language,
        )


def test_fallback_candidates_are_shown_for_manual_choice(tmp_path):
    media = tmp_path / "الهيبة.S01E03.mkv"
    media.touch()
    english = Candidate(
        provider=Provider.SUBDL,
        provider_id="en-1",
        release="Al Hayba S01E03 WEB-DL",
        language="en",
        score=88,
    )
    coordinator = FallbackCoordinator([], [english])
    app = SubsApp(
        config={
            "general": {
                "preferred_backend": "subdl",
                "skip_interactive_menu": True,
                "fallback_language": "en",
            },
            "subdl": {
                "api_key": "configured",
                "languages": {"Arabic": "ar", "English": "en"},
            },
        },
        media_paths=[str(media)],
        overrides={},
        coordinator=coordinator,
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)

            assert coordinator.fallback_calls == [("ar", "en", False)]
            assert [item.key for item in app.candidates] == [english.key]
            assert app.notice == "No ar subtitles found. Showing en results."
            assert app.state.queue[0].status is QueueStatus.AWAITING_PICK

    asyncio.run(run())


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
            # Columns are # Release L Source Fmt Flags D/L Fit in this mode.
            assert str(table.get_cell_at((0, 2))) == "EN"
            # The provider cell carries deliberate leading padding for the Source
            # column, so it is compared stripped.
            assert str(table.get_cell_at((0, 3))).strip() == "SubDL"
            assert table.max_scroll_x == 0

    asyncio.run(run())


DETAIL_EVIDENCE = (
    "Evidence:",
    "+ exact title",
    "+ year 1984",
    "+ BluRay",
    "+ 2160p",
    "- edition unknown",
    "- no hash match",
)

DETAIL_CONFLICT = (
    "Conflict:",
    "media: Theatrical",
    "subtitle: Director's Cut",
)


def _detail_candidate(**overrides) -> Candidate:
    values = {
        "provider": Provider.SUBDL,
        "provider_id": "arabic-ass",
        "release": "الهيبة.S01E03.WEB-DL",
        "language": "ar",
        "format": "ass",
        "score": 94,
        "compatibility": 94,
        "compatibility_badge": "BEST",
        "compatibility_evidence": DETAIL_EVIDENCE,
    }
    values.update(overrides)
    return Candidate(**values)


def test_detail_pane_shows_format_and_compatibility_evidence(configured_app):
    # The verbose badge and its evidence live here rather than in the table, so
    # this pane is the only place the user can see why a result outranks another.
    app, coordinator = configured_app
    coordinator.candidates = [_detail_candidate()]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            provider_line = str(app.query_one("#detail-provider", Static).content)
            detail = str(app.query_one("#detail-kv", Static).content)

            assert "ass" in provider_line
            assert "Compatibility: 94% BEST" in detail
            for line in DETAIL_EVIDENCE:
                assert line in detail

    asyncio.run(run())


def test_detail_pane_shows_a_conflict_block_instead_of_evidence(configured_app):
    # A known conflict is what the user must be able to see, so it replaces the
    # evidence block rather than sitting beside a misleading set of pluses.
    app, coordinator = configured_app
    coordinator.candidates = [
        _detail_candidate(
            compatibility=32,
            compatibility_badge="MISMATCH",
            compatibility_evidence=DETAIL_CONFLICT,
            compatibility_conflicts=DETAIL_CONFLICT[1:],
        )
    ]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            detail = str(app.query_one("#detail-kv", Static).content)

            assert "Compatibility: 32% MISMATCH" in detail
            assert "Conflict:" in detail
            assert "media: Theatrical" in detail
            assert "subtitle: Director's Cut" in detail

    asyncio.run(run())


def _preview_body(preview) -> str:
    """Rendered text of the candidate preview modal, in document order."""
    return "\n".join(str(widget.content) for widget in preview.query(Static))


def _preview_field(body: str, label: str) -> str:
    """Value rendered after ``label`` in the preview body, or "" when absent."""
    for line in body.splitlines():
        if line.startswith(f"{label} "):
            return line[len(label) :].strip()
    return ""


# The tallest evidence block the engine can emit: a header, a title signal, one
# signal per facet, and the hash signal. Building the modal from the maximum is
# what makes the clip guard below meaningful rather than a lucky fit.
PREVIEW_EVIDENCE = (
    "Evidence:",
    "+ exact title",
    "+ year 1984",
    "+ S01E03",
    "+ Director's Cut",
    "+ BluRay",
    "+ 2160p",
    "+ group GROUP",
    "+ H.264",
    "- edition unknown",
    "- no hash match",
)


def _preview_candidate() -> Candidate:
    return Candidate(
        provider=Provider.SUBDL,
        provider_id="arabic-ass",
        release="Amadeus.1984.1080p.BluRay.x264-GROUP",
        language="ar",
        format="ass",
        match_reasons=("release group match",),
        score=55,
        compatibility=96,
        compatibility_badge="BEST",
        compatibility_evidence=PREVIEW_EVIDENCE,
    )


def _rendered_rows(svg: str) -> str:
    """Text that reached the screen, as one searchable string.

    export_screenshot escapes spaces as ``&#160;``, so the rows are decoded
    before anything is searched for in them.
    """
    rows = [
        html.unescape(re.sub(r"<[^>]+>", "", row))
        for row in re.findall(r"<text[^>]*>(.*?)</text>", svg, flags=re.DOTALL)
    ]
    return "\n".join(rows).replace("\xa0", " ")


def test_candidate_preview_shows_format_and_compatibility_evidence(configured_app):
    # The modal is a second renderer of the same candidate as the detail pane and
    # was missed when the Fmt column and match reasons landed.
    app, coordinator = configured_app
    coordinator.candidates = [_preview_candidate()]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            app.action_preview()
            await pilot.pause()

            preview = app.screen
            assert isinstance(preview, CandidatePreview)
            body = _preview_body(preview)

            assert _preview_field(body, "Format") == "ass"
            assert _preview_field(body, "Compatibility:") == "96% BEST"
            # The modal and the detail pane read the same tuple, so the reasons the
            # user is shown cannot disagree with the order the rows arrived in.
            for line in PREVIEW_EVIDENCE:
                assert line in body

    asyncio.run(run())


def test_candidate_preview_renders_every_line_it_composes(configured_app):
    # The tail of the evidence block and the closing line are the first things a
    # too-short container drops, and export_screenshot is the only view of what
    # actually reached the screen.
    app, coordinator = configured_app
    coordinator.candidates = [_preview_candidate()]

    async def run():
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            app.action_preview()
            await pilot.pause()

            rendered = _rendered_rows(app.export_screenshot())

            assert "- no hash match" in rendered
            assert "esc or p to close" in rendered

    asyncio.run(run())
