"""Typed subtitle candidate table."""

from rich.text import Text
from textual import events
from textual.widgets import DataTable


class ResultsTable(DataTable):
    def __init__(self) -> None:
        super().__init__(
            id="results-table",
            cell_padding=0,
            cursor_background_priority="css",
            zebra_stripes=True,
        )
        self.cursor_type = "row"
        self._number_buffer = ""
        self._number_buffer_timer = None
        self._rendered_signature: tuple | None = None
        self._rendered_all_providers_mode: bool | None = None

    def on_mount(self) -> None:
        self._set_columns(all_providers_mode=False)

    def refresh_from_state(self, app) -> None:
        if self._rendered_all_providers_mode is not app.all_providers_mode:
            self.clear(columns=True)
            self._set_columns(app.all_providers_mode)
            self._rendered_signature = None
        signature = tuple(
            (
                candidate.key,
                candidate.release,
                candidate.format,
                candidate.language,
                candidate.provider,
                candidate.hash_match,
                candidate.hearing_impaired,
                candidate.ai_translated,
                candidate.download_count,
                candidate.compatibility,
            )
            for candidate in app.candidates
        )
        if signature == self._rendered_signature:
            self.sync_cursor(app.cursor_index)
            return
        self.clear()
        for index, candidate in enumerate(app.candidates, 1):
            flags = []
            if candidate.hash_match:
                flags.append("[blue]↓hash[/blue]")
            if candidate.hearing_impaired:
                flags.append("HI")
            if candidate.ai_translated:
                flags.append("[yellow]⚙AI[/yellow]")
            cells = [
                str(index),
                candidate.release,
                candidate.language.upper(),
                candidate.format or "",
                Text.from_markup(" ".join(flags)),
                _count(candidate.download_count),
                _fit(candidate.compatibility),
            ]
            if app.all_providers_mode:
                # Index 3, not 2: the columns are declared # Release L Source Fmt ...
                # so inserting at 2 put the provider label under "L" and the language
                # code under "Source".
                cells.insert(3, f" {candidate.provider.label}")
            self.add_row(*cells, key=candidate.key)
        self._rendered_signature = signature
        if self.row_count:
            self.sync_cursor(app.cursor_index)

    def sync_cursor(self, index: int) -> None:
        """Move the visual cursor without rebuilding candidate rows."""
        if not self.row_count:
            return
        row = min(max(index, 0), self.row_count - 1)
        if self.cursor_row != row:
            self.move_cursor(row=row)

    def on_key(self, event: events.Key) -> None:
        character = event.character
        if character is None or not character.isdecimal():
            return
        event.stop()
        self._number_buffer += character
        if self._number_buffer_timer is not None:
            self._number_buffer_timer.stop()
        self._number_buffer_timer = self.set_timer(
            0.8,
            self._clear_number_buffer,
        )
        result_number = int(self._number_buffer)
        if 1 <= result_number <= self.row_count:
            self.move_cursor(row=result_number - 1)

    def _clear_number_buffer(self) -> None:
        self._number_buffer = ""
        self._number_buffer_timer = None

    def _set_columns(self, all_providers_mode: bool) -> None:
        # Column widths are a budget, not a preference: #results-panel is 18fr against
        # DetailPane's 7fr (tui/style.tcss:495, :539), and at WIDE_LAYOUT_MIN_WIDTH=140
        # the table has 93 usable columns -- measured, not estimated: 93 fits with no
        # horizontal scroll and 94 scrolls by exactly one. "#" is 2 rather than 3
        # because rung 1 of the width ladder pays for the new column there, and it
        # loses nothing below 100 rows; Flags at 4 would clip "HI AI" and Release
        # cannot go below 71 (test_app.py asserts it). D/L must stay 5 (it renders
        # "48.2k") and Fit must stay 4, which is the whole width of "100%" -- the
        # percentage is not padded, so the column needs no more room than the
        # Match score it replaces.
        self.add_column("#", width=2)
        self.add_column("Release", width=62 if all_providers_mode else 71)
        self.add_column("L", width=2)
        if all_providers_mode:
            self.add_column("Source", width=9)
        self.add_column("Fmt", width=4)
        self.add_column("Flags", width=5)
        self.add_column("D/L", width=5)
        self.add_column("Fit", width=4)
        self._rendered_all_providers_mode = all_providers_mode


def _count(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def _fit(value: int) -> Text:
    return Text.from_markup(f"[yellow]{value}%[/yellow]")
