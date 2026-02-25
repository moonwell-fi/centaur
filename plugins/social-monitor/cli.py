"""CLI for social feed monitoring and career signal detection."""

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from ai_v2.cli_tables import Table

from .classifier import classify_unprocessed
from .db import (
    add_category,
    add_person,
    add_person_to_category,
    get_categories,
    get_db,
    get_people,
    get_unnotified_signals,
    import_people_csv,
)
from .digest import format_signal, send_digest
from .scanner import scan_all

app = typer.Typer(name="social-monitor", help="Social feed monitor for career signals")
console = Console()


@app.command("add-person")
def cmd_add_person(
    name: str = typer.Argument(..., help="Person's full name"),
    twitter: Optional[str] = typer.Option(None, "--twitter", "-t", help="Twitter handle"),
    linkedin: Optional[str] = typer.Option(None, "--linkedin", "-l", help="LinkedIn URL"),
    company: Optional[str] = typer.Option(None, "--company", "-c", help="Current company"),
    role: Optional[str] = typer.Option(None, "--role", "-r", help="Current role"),
    category: Optional[str] = typer.Option(None, "--category", help="Category to assign"),
) -> None:
    """Add a person to track."""
    conn = get_db()
    person_id = add_person(
        conn,
        name=name,
        twitter_handle=twitter,
        linkedin_url=linkedin,
        company=company,
        role=role,
    )
    console.print(f"[green]Added person: {name} (id={person_id})[/green]")

    if category:
        cats = conn.execute("SELECT id FROM categories WHERE name = ?", (category,)).fetchone()
        if cats:
            add_person_to_category(conn, person_id, cats["id"])
            console.print(f"  Assigned to category: {category}")
        else:
            console.print(f"[yellow]Category '{category}' not found. Skipping assignment.[/yellow]")
    conn.close()


@app.command("add-category")
def cmd_add_category(
    name: str = typer.Argument(..., help="Category name"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Description"),
) -> None:
    """Add a tracking category."""
    conn = get_db()
    cat_id = add_category(conn, name, description)
    console.print(f"[green]Added category: {name} (id={cat_id})[/green]")
    conn.close()


@app.command("import")
def cmd_import(
    csv_path: str = typer.Argument(..., help="Path to CSV file"),
    category_name: str = typer.Argument(..., help="Category to assign imported people to"),
) -> None:
    """Import people from CSV."""
    conn = get_db()
    count = import_people_csv(conn, csv_path, category_name)
    console.print(f"[green]Imported {count} people into category '{category_name}'.[/green]")
    conn.close()


@app.command("list-people")
def cmd_list_people(
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by category"),
) -> None:
    """List tracked people."""
    conn = get_db()
    people = get_people(conn, category=category)

    if not people:
        console.print("[yellow]No people found.[/yellow]")
        conn.close()
        return

    table = Table(title="Tracked People")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Twitter")
    table.add_column("Company")
    table.add_column("Role")

    for p in people:
        table.add_row(
            str(p["id"]),
            p["name"],
            f"@{p['twitter_handle']}" if p.get("twitter_handle") else "",
            p.get("company") or "",
            p.get("role") or "",
        )

    console.print(table)
    conn.close()


@app.command("list-categories")
def cmd_list_categories() -> None:
    """List categories."""
    conn = get_db()
    categories = get_categories(conn)

    if not categories:
        console.print("[yellow]No categories found.[/yellow]")
        conn.close()
        return

    table = Table(title="Categories")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Description")

    for c in categories:
        table.add_row(str(c["id"]), c["name"], c.get("description") or "")

    console.print(table)
    conn.close()


@app.command("scan")
def cmd_scan(
    limit: int = typer.Option(20, "--limit", "-n", help="Posts per person"),
) -> None:
    """Run Twitter scan for all tracked people."""
    conn = get_db()
    scan_all(conn, limit_per_person=limit)
    conn.close()


@app.command("classify")
def cmd_classify() -> None:
    """Run signal classification on unprocessed posts."""
    conn = get_db()
    classify_unprocessed(conn)
    conn.close()


@app.command("digest")
def cmd_digest(
    channel: Optional[str] = typer.Option(None, "--channel", "-c", help="Slack channel"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print digest without sending"),
) -> None:
    """Send daily digest."""
    conn = get_db()
    if dry_run:
        signals = get_unnotified_signals(conn, min_confidence=0.5)
        if not signals:
            console.print("[yellow]No signals to report.[/yellow]")
        else:
            today = datetime.now().strftime("%B %d, %Y")
            console.print("\n[bold]Social Feed Monitor — Daily Digest[/bold]")
            console.print(f"[dim]{today}[/dim]\n")
            console.print(f"{len(signals)} career signal(s) detected:\n")
            for signal in signals:
                console.print(format_signal(signal))
                console.print()
    else:
        send_digest(conn, channel=channel)
    conn.close()


@app.command("run")
def cmd_run(
    channel: Optional[str] = typer.Option(None, "--channel", "-c", help="Slack channel"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print digest without sending"),
    limit: int = typer.Option(20, "--limit", "-n", help="Posts per person"),
) -> None:
    """Full pipeline: scan + classify + digest."""
    conn = get_db()

    console.print("[bold]Step 1: Scanning Twitter feeds...[/bold]\n")
    scan_all(conn, limit_per_person=limit)

    console.print("\n[bold]Step 2: Classifying posts...[/bold]\n")
    classify_unprocessed(conn)

    console.print("\n[bold]Step 3: Sending digest...[/bold]\n")
    if dry_run:
        signals = get_unnotified_signals(conn, min_confidence=0.5)
        if not signals:
            console.print("[yellow]No signals to report.[/yellow]")
        else:
            today = datetime.now().strftime("%B %d, %Y")
            console.print("\n[bold]Social Feed Monitor — Daily Digest[/bold]")
            console.print(f"[dim]{today}[/dim]\n")
            console.print(f"{len(signals)} career signal(s) detected:\n")
            for signal in signals:
                console.print(format_signal(signal))
                console.print()
    else:
        send_digest(conn, channel=channel)

    conn.close()


@app.command("stats")
def cmd_stats() -> None:
    """Show database statistics."""
    conn = get_db()

    people_count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    posts_count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    signals_count = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE signal_type != 'NONE'"
    ).fetchone()[0]

    table = Table(title="Social Monitor Stats")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("People tracked", str(people_count))
    table.add_row("Posts fetched", str(posts_count))
    table.add_row("Signals detected", str(signals_count))

    console.print(table)

    categories = conn.execute(
        """SELECT c.name, COUNT(pc.person_id) AS count
           FROM categories c
           LEFT JOIN person_categories pc ON c.id = pc.category_id
           GROUP BY c.id
           ORDER BY c.name"""
    ).fetchall()

    if categories:
        cat_table = Table(title="By Category")
        cat_table.add_column("Category", style="bold")
        cat_table.add_column("People", justify="right")

        for row in categories:
            cat_table.add_row(row["name"], str(row["count"]))

        console.print(cat_table)

    conn.close()


@app.command("signals")
def cmd_signals(
    limit: int = typer.Option(20, "--limit", "-n", help="Max signals to show"),
    min_confidence: float = typer.Option(0.5, "--min-confidence", help="Minimum confidence"),
) -> None:
    """List recent signals."""
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, p.content AS post_content, p.post_url,
                  pe.name AS person_name, pe.twitter_handle, pe.company
           FROM signals s
           JOIN posts p ON s.post_id = p.id
           JOIN people pe ON p.person_id = pe.id
           WHERE s.signal_type != 'NONE' AND s.confidence >= ?
           ORDER BY s.created_at DESC
           LIMIT ?""",
        (min_confidence, limit),
    ).fetchall()

    if not rows:
        console.print("[yellow]No signals found.[/yellow]")
        conn.close()
        return

    table = Table(title="Recent Signals")
    table.add_column("ID", style="dim")
    table.add_column("Person", style="bold")
    table.add_column("Signal", style="bold red")
    table.add_column("Confidence", justify="right")
    table.add_column("Reasoning")
    table.add_column("Notified")

    for row in rows:
        table.add_row(
            str(row["id"]),
            row["person_name"],
            row["signal_type"],
            f"{row['confidence']:.0%}",
            (row.get("reasoning") or "")[:60],
            "✓" if row["notified"] else "",
        )

    console.print(table)
    conn.close()
