"""CLI for Sigma Computing."""

import json
import sys

from dotenv import load_dotenv

load_dotenv()

import typer
from rich.console import Console
from shared.cli_tables import Table

app = typer.Typer(name="sigma", help="Sigma Computing CLI for AI agents")
console = Console()


def _get_client():
    from .client import SigmaClient

    return SigmaClient()


@app.command()
def workbooks(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List workbooks."""
    client = _get_client()
    items = client.list_workbooks(limit=limit)

    if json_output:
        print(json.dumps(items, indent=2, ensure_ascii=False), file=sys.stdout)
        raise typer.Exit()

    if not items:
        console.print("[yellow]No workbooks found.[/]")
        raise typer.Exit()

    table = Table(title=f"Workbooks ({len(items)})")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Name", style="cyan", max_width=40)
    table.add_column("Owner", style="white", max_width=30)
    table.add_column("Updated", style="green", max_width=20)

    for item in items:
        workbook_id = item.get("workbookId", "")
        name = item.get("name", "")
        owner = item.get("ownerId", "")[:20] if item.get("ownerId") else ""
        updated = item.get("updatedAt", "")[:10] if item.get("updatedAt") else ""
        table.add_row(workbook_id[:12] + "...", name[:40], owner, updated)

    console.print(table)


@app.command()
def workbook(
    workbook_id: str = typer.Argument(..., help="Workbook ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get workbook details."""
    client = _get_client()
    item = client.get_workbook(workbook_id)

    if json_output:
        print(json.dumps(item, indent=2, ensure_ascii=False), file=sys.stdout)
        raise typer.Exit()

    console.print("[bold]Workbook Details[/]\n")
    console.print(f"[cyan]ID:[/] {item.get('workbookId', '')}")
    console.print(f"[cyan]Name:[/] {item.get('name', '')}")
    console.print(f"[cyan]Owner:[/] {item.get('ownerId', '')}")
    console.print(f"[cyan]URL:[/] {item.get('url', '')}")
    console.print(f"[cyan]Created:[/] {item.get('createdAt', '')}")
    console.print(f"[cyan]Updated:[/] {item.get('updatedAt', '')}")


@app.command()
def pages(
    workbook_id: str = typer.Argument(..., help="Workbook ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List pages in a workbook."""
    client = _get_client()
    items = client.list_pages(workbook_id)

    if json_output:
        print(json.dumps(items, indent=2, ensure_ascii=False), file=sys.stdout)
        raise typer.Exit()

    if not items:
        console.print("[yellow]No pages found.[/]")
        raise typer.Exit()

    table = Table(title=f"Pages ({len(items)})")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Name", style="cyan", max_width=50)
    table.add_column("Type", style="green", max_width=15)

    for item in items:
        page_id = item.get("pageId", "")
        name = item.get("name", "")
        page_type = item.get("type", "")
        table.add_row(page_id[:12] + "..." if len(page_id) > 12 else page_id, name, page_type)

    console.print(table)


@app.command()
def members(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List organization members."""
    client = _get_client()
    items = client.list_members(limit=limit)

    if json_output:
        print(json.dumps(items, indent=2, ensure_ascii=False), file=sys.stdout)
        raise typer.Exit()

    if not items:
        console.print("[yellow]No members found.[/]")
        raise typer.Exit()

    table = Table(title=f"Members ({len(items)})")
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Email", style="cyan", max_width=35)
    table.add_column("Name", style="white", max_width=25)
    table.add_column("Type", style="green", max_width=15)

    for item in items:
        member_id = item.get("memberId", "")
        email = item.get("email", "")
        name = f"{item.get('firstName', '')} {item.get('lastName', '')}".strip()
        member_type = item.get("memberType", "")
        table.add_row(member_id[:12] + "...", email, name, member_type)

    console.print(table)


@app.command()
def teams(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List teams."""
    client = _get_client()
    items = client.list_teams(limit=limit)

    if json_output:
        print(json.dumps(items, indent=2, ensure_ascii=False), file=sys.stdout)
        raise typer.Exit()

    if not items:
        console.print("[yellow]No teams found.[/]")
        raise typer.Exit()

    table = Table(title=f"Teams ({len(items)})")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Name", style="cyan", max_width=40)
    table.add_column("Members", style="green", max_width=10)

    for item in items:
        team_id = item.get("teamId", "")
        name = item.get("name", "")
        member_count = str(item.get("memberCount", ""))
        table.add_row(team_id[:12] + "...", name, member_count)

    console.print(table)


@app.command("embed-url")
def embed_url(
    workbook_id: str = typer.Argument(..., help="Workbook ID"),
    email: str = typer.Option(..., "--email", "-e", help="User email for embed session"),
    account_type: str = typer.Option(
        "viewer", "--type", "-t", help="Account type (viewer/creator)"
    ),
    teams: str = typer.Option(None, "--teams", help="Comma-separated team names"),
    session_length: int = typer.Option(3600, "--session", "-s", help="Session length in seconds"),
):
    """Generate embed URL for a workbook."""
    client = _get_client()

    teams_list = [t.strip() for t in teams.split(",")] if teams else None

    url = client.generate_embed_url(
        workbook_id=workbook_id,
        email=email,
        account_type=account_type,
        teams=teams_list,
        session_length=session_length,
    )

    console.print("[bold]Embed URL[/]\n")
    console.print(url)


@app.command()
def raw(
    endpoint: str = typer.Argument(..., help="API endpoint (e.g., /workbooks)"),
    method: str = typer.Option("GET", "--method", "-X", help="HTTP method"),
    body: str = typer.Option(None, "--data", "-d", help="Request body as JSON"),
):
    """Make raw API call."""
    client = _get_client()

    body_dict = json.loads(body) if body else None
    result = client.raw_request(method, endpoint, body_dict)
    print(json.dumps(result, indent=2, ensure_ascii=False), file=sys.stdout)


if __name__ == "__main__":
    app()
