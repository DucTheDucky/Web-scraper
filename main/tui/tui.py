import asyncio
import sys
from pathlib import Path

from textual.containers import Vertical
from textual.widgets import DataTable, Header, Footer, Label

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable

# Allow direct launch from this nested folder or any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlserver_store import connect_database


def read_properties():
    connection = connect_database()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT TOP (200) Post_ID, Title, Price_Text,
                   Price_VND, Price_Per_M2, Area
            FROM dbo.batdongsan_data
            ORDER BY Scraped_At DESC, Post_ID
        """)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


class TableApp(App):
    BINDINGS = [("q", "quit", "Quit")]
    CSS_PATH = "tui.tcss"

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="table-panel"):
            yield Label("Scraped properties", id="table-title")
            yield DataTable(id="property-table")

        yield Footer()
    def on_mount(self) -> None:
        self.loading = False

        table = self.query_one(DataTable)
        table.add_columns(
            "Post ID", "Title", "Price text",
            "Total VND", "VND/m2", "Area"
        )

        self.set_timer(0.1, self.reload_table)
        self.set_interval(3, self.reload_table)
        

    async def reload_table(self) -> None:
        if self.loading:
            return

        self.loading = True
        try:
            rows = await asyncio.to_thread(read_properties)

            table = self.query_one(DataTable)
            table.clear()

            for row in rows:
                cells = [
                    Text(
                        "" if cell is None else str(cell),
                        style="#A36053"
                    )
                    for cell in row
                ]
                table.add_row(*cells, key=str(row[0]))

        except Exception as error:
            self.notify(
                str(error),
                title="Database read failed",
                severity="error"
            )
        finally:
            self.loading = False


if __name__ == "__main__":
    TableApp(ansi_color=True).run()
