"""CLI entrypoint for ParadigmDB."""

from dotenv import load_dotenv

load_dotenv()

import typer
from rich.console import Console
from shared.cli_tables import Table

app = typer.Typer(name="paradigmdb", help="Paradigm internal database, Shift notes, BigQuery")
console = Console()


@app.command()
def db(
    query: str = typer.Argument(None, help="SQL query to execute"),
    tables: bool = typer.Option(False, "--tables", "-t", help="List all tables"),
    describe: str = typer.Option(None, "--describe", "-d", help="Describe a table"),
    funds: bool = typer.Option(False, "--funds", "-f", help="List funds"),
    assets: bool = typer.Option(False, "--assets", "-a", help="List assets"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
    tunnel: bool = typer.Option(False, "--tunnel", help="Start persistent SSH tunnel"),
    close: bool = typer.Option(False, "--close", help="Close persistent SSH tunnel"),
):
    """Query Paradigm's internal PostgreSQL database.

    The tunnel is started automatically on first query and persists across commands.
    Use --close to stop the tunnel when done.
    """
    from .database import get_db, is_tunnel_running, start_persistent_tunnel, stop_persistent_tunnel

    if close:
        if stop_persistent_tunnel():
            console.print("[green]SSH tunnel closed.[/]")
        else:
            console.print("[yellow]No tunnel running.[/]")
        return

    if tunnel:
        if is_tunnel_running():
            console.print("[green]SSH tunnel already running.[/]")
        else:
            start_persistent_tunnel()
            console.print("[green]SSH tunnel started.[/]")
        return

    # Auto-start tunnel if not running
    if not is_tunnel_running():
        console.print("[dim]Starting SSH tunnel...[/]")
        start_persistent_tunnel()

    db = get_db()

    try:
        if tables:
            table_list = db.list_tables()
            table = Table(title="Database Tables")
            table.add_column("Table Name", style="cyan")
            for t in table_list:
                table.add_row(t)
            console.print(table)

        elif describe:
            cols = db.describe_table(describe)
            if not cols:
                console.print(f"[yellow]Table '{describe}' not found.[/]")
                return
            table = Table(title=f"Table: {describe}")
            table.add_column("Column", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Nullable", style="dim")
            for c in cols:
                table.add_row(c["column_name"], c["data_type"], c["is_nullable"])
            console.print(table)

        elif funds:
            results = db.get_funds(limit=limit)
            table = Table(title="Funds")
            if results:
                for key in results[0].keys():
                    table.add_column(str(key), max_width=30)
                for r in results:
                    table.add_row(*[str(v)[:30] for v in r.values()])
            console.print(table)

        elif assets:
            results = db.get_assets(limit=limit)
            table = Table(title="Assets")
            if results:
                for key in list(results[0].keys())[:6]:
                    table.add_column(str(key), max_width=25)
                for r in results:
                    table.add_row(*[str(v)[:25] for v in list(r.values())[:6]])
            console.print(table)

        elif query:
            results = db.query(query)
            if not results:
                console.print("[yellow]No results.[/]")
                return
            table = Table(title="Query Results")
            for key in list(results[0].keys())[:8]:
                table.add_column(str(key), max_width=30)
            for r in results[:limit]:
                table.add_row(*[str(v)[:30] for v in list(r.values())[:8]])
            console.print(table)
            if len(results) > limit:
                console.print(f"[dim]... showing {limit} of {len(results)} results[/]")

        else:
            console.print(
                "[yellow]Provide a query or use --tables, --describe, --funds, --assets[/]"
            )

    except Exception as e:
        error_str = str(e).lower()
        console.print(f"[red]Database error: {e}[/]")
        # Only suggest tunnel check for connection errors, not schema/SQL errors
        if any(
            hint in error_str
            for hint in ["connection refused", "could not connect", "timeout", "no route"]
        ):
            console.print("[dim]Make sure the bastion SSH tunnel is running.[/]")


@app.command()
def bq(
    query: str = typer.Argument(None, help="BigQuery SQL query to execute"),
    tables: bool = typer.Option(False, "--tables", "-t", help="List all tables/views"),
    describe: str = typer.Option(None, "--describe", "-d", help="Describe a table/view"),
    ticker: str = typer.Option(None, "--ticker", help="Filter transactions by ticker symbol"),
    fund: str = typer.Option(None, "--fund", "-f", help="Filter by fund (PF, P1, P2)"),
    txn_type: str = typer.Option(None, "--type", help="Filter by transaction type"),
    start_date: str = typer.Option(None, "--start", help="Start date (YYYY-MM-DD)"),
    end_date: str = typer.Option(None, "--end", help="End date (YYYY-MM-DD)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """Query BigQuery views in custody-dashboard.shift_prod_public_views.

    Examples:
        paradigmdb bq --tables                                  # List available views
        paradigmdb bq -d transactions_csv                       # Describe transactions table
        paradigmdb bq --ticker HYPE --start 2026-01-01          # HYPE transactions in 2026
        paradigmdb bq --ticker HYPE --type staking              # HYPE staking rewards
        paradigmdb bq "SELECT * FROM transactions_csv LIMIT 5"  # Raw SQL query
    """
    from .bigquery import describe_table, get_transactions, list_tables, query_bigquery

    try:
        if tables:
            table_list = list_tables()
            table = Table(title="BigQuery Views (custody-dashboard.shift_prod_public_views)")
            table.add_column("Table/View Name", style="cyan")
            for t in table_list:
                table.add_row(t)
            console.print(table)

        elif describe:
            cols = describe_table(describe)
            if not cols:
                console.print(f"[yellow]Table '{describe}' not found.[/]")
                return
            table = Table(title=f"Table: {describe}")
            table.add_column("Column", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Mode", style="dim")
            for c in cols:
                table.add_row(c["column_name"], c["data_type"], c["mode"])
            console.print(table)

        elif ticker or fund or txn_type or start_date or end_date:
            results = get_transactions(
                ticker=ticker,
                fund=fund,
                transaction_type=txn_type,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
            if not results:
                console.print("[yellow]No transactions found.[/]")
                return
            table = Table(title="Transactions")
            for key in list(results[0].keys())[:8]:
                table.add_column(str(key), max_width=25)
            for r in results[:limit]:
                table.add_row(*[str(v)[:25] for v in list(r.values())[:8]])
            console.print(table)

        elif query:
            full_query = query
            if "FROM " in query.upper() and "`" not in query:
                import re

                full_query = re.sub(
                    r"\bFROM\s+(\w+)",
                    r"FROM `custody-dashboard.shift_prod_public_views.\1`",
                    query,
                    flags=re.IGNORECASE,
                )
            results = query_bigquery(full_query, limit)
            if not results:
                console.print("[yellow]No results.[/]")
                return
            table = Table(title="Query Results")
            for key in list(results[0].keys())[:8]:
                table.add_column(str(key), max_width=30)
            for r in results[:limit]:
                table.add_row(*[str(v)[:30] for v in list(r.values())[:8]])
            console.print(table)

        else:
            console.print(
                "[yellow]Provide a query, --tables, --describe, or transaction filters[/]"
            )

    except Exception as e:
        console.print(f"[red]BigQuery error: {e}[/]")
        console.print("[dim]Ensure svc_ai@paradigm.xyz has BigQuery access to custody-dashboard[/]")


@app.command()
def notes(
    query: str = typer.Argument(None, help="Search query (omit to list recent notes)"),
    note_type: str = typer.Option(
        None,
        "--type",
        "-t",
        help="Filter by type: OPPORTUNITY, PORTCO_UPDATE, PORTCO_REVIEW, TALENT, GTM, etc.",
    ),
    org: str = typer.Option(None, "--org", "-o", help="Filter by organization name"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
    read: str = typer.Option(None, "--read", "-r", help="Read full note by ID"),
    stats: bool = typer.Option(False, "--stats", "-s", help="Show note statistics"),
    authors: bool = typer.Option(False, "--authors", "-a", help="Show top authors"),
    full: bool = typer.Option(False, "--full", "-f", help="Show full note text (not truncated)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Search and read Shift notes from the investment process.

    Note types:
        OPPORTUNITY     - Investment opportunities
        PORTCO_UPDATE   - Portfolio company updates
        PORTCO_REVIEW   - Portfolio company reviews
        TALENT          - Hiring/recruiting notes
        GTM             - Go-to-market notes
        DESIGN          - Design team notes
        LEGAL_POLICY    - Legal/policy notes
        OTHER           - Miscellaneous

    Examples:
        paradigmdb notes                           # Recent notes
        paradigmdb notes "uniswap"                 # Search for Uniswap
        paradigmdb notes -t OPPORTUNITY            # Investment opportunities
        paradigmdb notes -o "Uniswap" -n 10        # Notes about Uniswap org
        paradigmdb notes --read abc123             # Read full note by ID
        paradigmdb notes --stats                   # Note statistics
        paradigmdb notes --authors                 # Top note authors
    """
    import json
    import sys

    from .database import is_tunnel_running, start_persistent_tunnel
    from .notes import get_notes_client

    # Auto-start tunnel if not running
    if not is_tunnel_running():
        console.print("[dim]Starting SSH tunnel...[/]")
        start_persistent_tunnel()

    client = get_notes_client()

    # Read a specific note
    if read:
        data = client.get_note_with_relations(read)
        if not data:
            console.print(f"[red]Note '{read}' not found.[/]")
            return

        note = data["note"]

        if json_output:
            print(
                json.dumps(
                    {
                        "id": note.id,
                        "title": note.title,
                        "type": note.note_type,
                        "source": note.source,
                        "created_at": note.created_at.isoformat(),
                        "created_by": note.created_by_name,
                        "organizations": data["organizations"],
                        "people": data["people"],
                        "notes": note.notes,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                file=sys.stdout,
            )
            return

        console.print(f"\n[bold cyan]{note.title or '(Untitled)'}[/]\n")
        console.print(f"[dim]Type:[/] {note.note_type or 'N/A'}")
        console.print(f"[dim]Source:[/] {note.source}")
        console.print(f"[dim]Created:[/] {note.created_at.strftime('%Y-%m-%d %H:%M')}")
        console.print(f"[dim]Author:[/] {note.created_by_name or note.created_by_id}")
        if data["organizations"]:
            console.print(f"[dim]Organizations:[/] {', '.join(data['organizations'])}")
        if data["people"]:
            console.print(f"[dim]People:[/] {', '.join(data['people'])}")
        console.print(f"\n{note.notes}")
        return

    # Show statistics
    if stats:
        s = client.get_stats()

        if json_output:
            print(json.dumps(s, indent=2), file=sys.stdout)
            return

        console.print("\n[bold]Shift Notes Statistics[/]\n")
        console.print(f"Total notes: [cyan]{s['total']:,}[/]")
        console.print(f"Last 30 days: [green]{s['last_30_days']:,}[/]\n")

        table = Table(title="By Type")
        table.add_column("Type", style="cyan")
        table.add_column("Count", justify="right", style="green")
        for t, c in sorted(s["by_type"].items(), key=lambda x: -x[1]):
            if t:
                table.add_row(t, f"{c:,}")
        console.print(table)

        table2 = Table(title="By Source")
        table2.add_column("Source", style="cyan")
        table2.add_column("Count", justify="right", style="green")
        for src, c in s["by_source"].items():
            table2.add_row(src, f"{c:,}")
        console.print(table2)
        return

    # Show top authors
    if authors:
        author_list = client.get_authors(limit=limit)

        if json_output:
            print(json.dumps(author_list, indent=2, default=str), file=sys.stdout)
            return

        table = Table(title="Top Note Authors")
        table.add_column("Name", style="cyan", max_width=25)
        table.add_column("Email", style="dim", max_width=30)
        table.add_column("Notes", justify="right", style="green")

        for a in author_list:
            table.add_row(a["name"] or "", a["email"] or "", f"{a['note_count']:,}")

        console.print(table)
        return

    # Search or list notes
    if org:
        notes_list = client.get_notes_for_organization(org, limit=limit)
        title = f"Notes for '{org}'"
    elif query:
        notes_list = client.search_notes(query, note_type=note_type, limit=limit)
        title = f"Notes matching '{query}'"
    else:
        notes_list = client.list_notes(note_type=note_type, limit=limit)
        title = "Recent Notes" + (f" ({note_type})" if note_type else "")

    if not notes_list:
        console.print("[yellow]No notes found.[/]")
        return

    if json_output:
        print(
            json.dumps(
                [
                    {
                        "id": n.id,
                        "title": n.title,
                        "type": n.note_type,
                        "created_at": n.created_at.isoformat(),
                        "created_by": n.created_by_name,
                        "notes": n.notes if full else n.notes[:200],
                    }
                    for n in notes_list
                ],
                indent=2,
                ensure_ascii=False,
            ),
            file=sys.stdout,
        )
        return

    if full:
        console.print(f"\n[bold]{title}[/]\n")
        for n in notes_list:
            console.print(f"[cyan]{n.title or '(Untitled)'}[/] [{n.note_type or 'N/A'}]")
            created = n.created_at.strftime("%Y-%m-%d")
            author = n.created_by_name or "Unknown"
            console.print(f"[dim]{created} by {author}[/]")
            console.print(f"[dim]ID: {n.id}[/]")
            console.print(n.notes)
            console.print()
        return

    table = Table(title=title)
    table.add_column("Title", style="cyan", max_width=40)
    table.add_column("Type", style="green", max_width=15)
    table.add_column("Date", style="dim", max_width=12)
    table.add_column("Author", style="dim", max_width=15)
    table.add_column("ID", style="dim", max_width=12)

    for n in notes_list:
        title_str = (n.title or n.notes[:40])[:40]
        if len(n.title or n.notes) > 40:
            title_str += "..."
        author = (n.created_by_name or "")[:15]
        table.add_row(
            title_str,
            n.note_type or "",
            n.created_at.strftime("%Y-%m-%d"),
            author,
            n.id[:12] + "...",
        )

    console.print(table)
    console.print("\n[dim]Use --read <ID> to read full note[/]")


if __name__ == "__main__":
    app()
