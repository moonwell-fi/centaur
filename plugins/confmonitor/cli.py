"""CLI for conference date monitoring."""
from dotenv import load_dotenv

load_dotenv()

import json
import re
import subprocess
from datetime import datetime
from typing import Optional

import httpx
import typer
from rich.console import Console
from ai_v2.cli_tables import Table

app = typer.Typer(
    name="confmonitor",
    help="Conference date monitor - checks for new 2026 conference dates",
)
console = Console()

SPREADSHEET_ID = "1AgNeNaIVgWl7jIovJsvW-F1zIz150e-nCr4VCE56odE"

CONFERENCE_URLS = {
    "ETHRiyadh": "https://ethriyadh.io",
    "Prediction Markets Conference": "https://www.predictionmarketsconference.com",
    "Berlin Blockchain Week": "https://blockchainweek.berlin",
    "Solana Crossroads": "https://crossroads.solana.com",
    "Solana APEX": "https://apex.solana.com",
    "ETHTaipei": "https://ethtaipei.org",
    "Columbia Crypto Economics": "https://economics.engineering.columbia.edu/blockchain",
    "Tokenized Live": "https://tokenized.live",
    "Ondo Summit": "https://summit.ondo.finance",
    "Sequoia AI Ascent": "https://www.sequoiacap.com/ai-ascent",
    "Stripe Sessions": "https://stripe.com/sessions",
    "Stripe Tour": "https://stripe.com/tour",
    "Fintech NerdCon 2026": "https://fintechnerdcon.com",
    "Hill and Valley Forum": "https://hillandvalleyforum.com",
    "Blockchain Futurist Conference Florida": "https://futuristconference.com",
    "Avalanche Summit": "https://summit.avax.network",
    "Permissionless": "https://blockworks.co/event/permissionless",
    "Korea Blockchain Week": "https://koreablockchainweek.com",
    "NeurIPS": "https://nips.cc",
    "OpenAI Dev Day": "https://openai.com/devday",
    "Manifest": "https://manifest.is",
    "USC Blockchain Conference": "https://blockchain.usc.edu",
}


def run_gsuite_cmd(args: list[str]) -> str:
    """Run gsuite CLI command and return output."""
    result = subprocess.run(
        ["gsuite", "-a", "svc_ai@paradigm.xyz"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gsuite command failed: {result.stderr}")
    return result.stdout


def run_slack_cmd(args: list[str]) -> str:
    """Run slack CLI command and return output."""
    result = subprocess.run(["slack"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"slack command failed: {result.stderr}")
    return result.stdout


def get_sheet_data() -> list[dict]:
    """Read conference data from the Google Sheet."""
    output = run_gsuite_cmd(["sheets", "read", SPREADSHEET_ID, "--json"])
    return json.loads(output)


def search_conference_dates(conference_name: str) -> Optional[dict]:
    """Search the web for conference 2026 dates.

    Returns dict with start_date, end_date, location if found.
    """
    search_query = f"{conference_name} 2026 dates location"

    try:
        response = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": search_query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        text = response.text.lower()

        date_patterns = [
            r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,?\s*2026",
            r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+2026",
            r"2026[-/](\d{2})[-/](\d{2})",
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                return {"raw_match": match.group(0), "source": "web_search"}

        return None
    except Exception as e:
        console.print(f"[dim]Search error for {conference_name}: {e}[/]")
        return None


def check_conference_website(conference_name: str, url: str) -> Optional[dict]:
    """Check a conference website directly for 2026 dates."""
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
            follow_redirects=True,
        )
        text = response.text

        date_patterns = [
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,?\s*2026",
            r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+2026",
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return {"raw_match": match.group(0), "source": url}

        return None
    except Exception as e:
        console.print(f"[dim]Website check error for {url}: {e}[/]")
        return None


def update_sheet_cell(row_index: int, column: str, value: str) -> None:
    """Update a specific cell in the sheet."""
    col_map = {
        "Quarter": "C",
        "Start Date": "D",
        "End Date": "E",
        "Location": "F",
        "Notes": "H",
    }
    col_letter = col_map.get(column, "H")
    cell_range = f"{col_letter}{row_index + 2}"

    run_gsuite_cmd(["sheets", "update", SPREADSHEET_ID, cell_range, json.dumps([[value]])])


def send_slack_notification(updates: list[dict], channel: str = "ai-agent-administration") -> None:
    """Send Slack notification about found conference dates."""
    if not updates:
        return

    message_lines = ["*🗓️ Conference Date Updates Found*\n"]
    for update in updates:
        message_lines.append(
            f"• *{update['conference']}*: {update['dates']} (source: {update['source']})"
        )

    message = "\n".join(message_lines)
    message += "\n\n<@U03RE7C21RL>"

    run_slack_cmd(["send", f"#{channel}", message])


@app.command("check")
def check_dates(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Don't update sheet or notify"),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Send Slack notification"),
    channel: str = typer.Option("ai-agent-administration", "--channel", "-c", help="Slack channel"),
):
    """Check all conferences with TBA dates for new 2026 date announcements."""
    console.print("[bold]Checking conference dates...[/]")

    try:
        rows = get_sheet_data()
    except Exception as e:
        console.print(f"[red]Failed to read spreadsheet: {e}[/]")
        raise typer.Exit(1)

    tba_conferences = []
    for i, row in enumerate(rows):
        event_name = row.get("Event Name", "")
        quarter = row.get("Quarter", "")
        start_date = row.get("Start Date", "")

        if "TBA" in quarter.upper() or (not start_date and event_name):
            tba_conferences.append({"index": i, "name": event_name, "row": row})

    console.print(f"[cyan]Found {len(tba_conferences)} conferences with TBA dates[/]")

    updates = []

    for conf in tba_conferences:
        name = conf["name"]
        if not name:
            continue

        console.print(f"[dim]Checking {name}...[/]")

        result = None
        url = CONFERENCE_URLS.get(name)
        if url:
            result = check_conference_website(name, url)

        if not result:
            result = search_conference_dates(name)

        if result:
            console.print(f"[green]✓ Found dates for {name}: {result['raw_match']}[/]")
            updates.append(
                {
                    "conference": name,
                    "dates": result["raw_match"],
                    "source": result["source"],
                    "row_index": conf["index"],
                }
            )

    if updates:
        table = Table(title=f"Found {len(updates)} Date Updates")
        table.add_column("Conference", style="cyan")
        table.add_column("Dates Found", style="green")
        table.add_column("Source", style="dim")

        for update in updates:
            table.add_row(update["conference"], update["dates"], update["source"][:40])

        console.print(table)

        if not dry_run:
            console.print("\n[bold]Updating spreadsheet...[/]")
            for update in updates:
                try:
                    update_sheet_cell(
                        update["row_index"],
                        "Notes",
                        f"Dates found: {update['dates']} (auto-detected {datetime.now().strftime('%Y-%m-%d')})",
                    )
                    console.print(f"[green]✓ Updated {update['conference']}[/]")
                except Exception as e:
                    console.print(f"[red]Failed to update {update['conference']}: {e}[/]")

            if notify:
                console.print("\n[bold]Sending Slack notification...[/]")
                try:
                    send_slack_notification(updates, channel)
                    console.print("[green]✓ Notification sent[/]")
                except Exception as e:
                    console.print(f"[red]Failed to send notification: {e}[/]")
        else:
            console.print("[yellow]Dry run - no changes made[/]")
    else:
        console.print("[yellow]No new dates found[/]")


@app.command("list-tba")
def list_tba():
    """List all conferences with TBA dates."""
    try:
        rows = get_sheet_data()
    except Exception as e:
        console.print(f"[red]Failed to read spreadsheet: {e}[/]")
        raise typer.Exit(1)

    table = Table(title="Conferences with TBA Dates")
    table.add_column("Event Name", style="cyan")
    table.add_column("Quarter", style="yellow")
    table.add_column("Category", style="dim")

    count = 0
    for row in rows:
        event_name = row.get("Event Name", "")
        quarter = row.get("Quarter", "")
        start_date = row.get("Start Date", "")
        category = row.get("Ecosystem/Category", "")

        if "TBA" in quarter.upper() or (not start_date and event_name):
            table.add_row(event_name, quarter, category)
            count += 1

    console.print(table)
    console.print(f"\n[dim]Total: {count} conferences with TBA dates[/]")


@app.command("test-sheet")
def test_sheet():
    """Test reading the conference spreadsheet."""
    try:
        rows = get_sheet_data()
        console.print(f"[green]✓ Successfully read {len(rows)} rows from spreadsheet[/]")

        table = Table(title="First 5 Conferences")
        table.add_column("Event Name", style="cyan")
        table.add_column("Quarter", style="yellow")
        table.add_column("Start Date")
        table.add_column("Location")

        for row in rows[:5]:
            table.add_row(
                row.get("Event Name", "")[:30],
                row.get("Quarter", ""),
                row.get("Start Date", ""),
                row.get("Location", "")[:20],
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Failed to read spreadsheet: {e}[/]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
