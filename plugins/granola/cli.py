"""Granola CLI for AI agents."""

from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from shared.cli_tables import Table

app = typer.Typer(name="granola", help="Query Granola meeting notes and transcripts")
console = Console()


def _format_date(date_str: str | None) -> str:
    """Format ISO date string to readable format."""
    if not date_str:
        return "-"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return date_str[:16] if date_str else "-"


@app.command("list")
def list_notes(
    limit: int = typer.Option(20, "--limit", "-n", help="Max notes to return"),
    full: bool = typer.Option(False, "--full", "-f", help="Show full titles"),
):
    """List recent meeting notes."""
    from .client import GranolaClient

    client = GranolaClient()
    docs = client.list_documents(limit=limit)

    if not docs:
        console.print("[yellow]No meeting notes found.[/yellow]")
        return

    table = Table(title=f"Granola Notes ({len(docs)})")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Title", style="cyan", max_width=None if full else 50)
    table.add_column("Date", style="green")

    for doc in docs:
        doc_id = doc.get("id", "")[:12]
        title = doc.get("title", "Untitled")
        if not full and len(title) > 47:
            title = title[:47] + "..."
        created = _format_date(doc.get("created_at"))
        table.add_row(doc_id, title, created)

    console.print(table)


@app.command("get")
def get_note(
    doc_id: str = typer.Argument(..., help="Document ID (full or partial)"),
    raw: bool = typer.Option(False, "--raw", "-r", help="Output raw markdown"),
):
    """Get a specific meeting note by ID."""
    from .client import GranolaClient

    client = GranolaClient()

    if len(doc_id) < 36:
        docs = client.list_documents(limit=100)
        matches = [d for d in docs if d.get("id", "").startswith(doc_id)]
        if not matches:
            console.print(f"[red]No document found matching: {doc_id}[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(f"[yellow]Multiple matches for '{doc_id}':[/yellow]")
            for m in matches[:5]:
                console.print(f"  {m.get('id')} - {m.get('title', 'Untitled')}")
            raise typer.Exit(1)
        doc_id = matches[0]["id"]

    doc = client.get_document(doc_id)
    title = doc.get("title", "Untitled")
    created = _format_date(doc.get("created_at"))
    content = client.extract_notes_content(doc)

    if raw:
        print(f"# {title}\n")
        print(f"*{created}*\n")
        print(content)
    else:
        console.print(Panel(f"[bold]{title}[/bold]\n[dim]{created}[/dim]"))
        if content:
            console.print(Markdown(content))
        else:
            console.print("[yellow]No notes content available.[/yellow]")


@app.command("transcript")
def get_transcript(
    doc_id: str = typer.Argument(..., help="Document ID"),
    speakers: bool = typer.Option(True, "--speakers/--no-speakers", help="Show speaker names"),
):
    """Get the transcript for a meeting note."""
    from .client import GranolaClient

    client = GranolaClient()

    if len(doc_id) < 36:
        docs = client.list_documents(limit=100)
        matches = [d for d in docs if d.get("id", "").startswith(doc_id)]
        if not matches:
            console.print(f"[red]No document found matching: {doc_id}[/red]")
            raise typer.Exit(1)
        doc_id = matches[0]["id"]

    try:
        transcript = client.get_transcript(doc_id)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    utterances = transcript.get("utterances", [])
    if not utterances:
        console.print("[yellow]Transcript is empty.[/yellow]")
        return

    for utt in utterances:
        speaker = utt.get("speaker", "Unknown")
        text = utt.get("text", "")
        if speakers:
            console.print(f"[bold cyan]{speaker}:[/bold cyan] {text}")
        else:
            console.print(text)


@app.command("search")
def search_notes(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """Search meeting notes by title."""
    from .client import GranolaClient

    client = GranolaClient()
    docs = client.list_documents(limit=100)

    query_lower = query.lower()
    matches = [d for d in docs if query_lower in d.get("title", "").lower()][:limit]

    if not matches:
        console.print(f"[yellow]No notes matching: {query}[/yellow]")
        return

    table = Table(title=f"Search: '{query}' ({len(matches)} results)")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Title", style="cyan")
    table.add_column("Date", style="green")

    for doc in matches:
        doc_id = doc.get("id", "")[:12]
        title = doc.get("title", "Untitled")
        created = _format_date(doc.get("created_at"))
        table.add_row(doc_id, title, created)

    console.print(table)


@app.command("workspaces")
def list_workspaces():
    """List all workspaces (organizations)."""
    from .client import GranolaClient

    client = GranolaClient()
    workspaces = client.list_workspaces()

    if not workspaces:
        console.print("[yellow]No workspaces found.[/yellow]")
        return

    table = Table(title="Workspaces")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="cyan")

    for ws in workspaces:
        table.add_row(ws.get("id", ""), ws.get("name", ""))

    console.print(table)


@app.command("folders")
def list_folders():
    """List all folders (document lists)."""
    from .client import GranolaClient

    client = GranolaClient()
    folders = client.list_folders()

    if not folders:
        console.print("[yellow]No folders found.[/yellow]")
        return

    table = Table(title="Folders")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Documents", style="green", justify="right")

    for folder in folders:
        doc_count = len(folder.get("document_ids", []))
        table.add_row(
            folder.get("id", ""),
            folder.get("name", ""),
            str(doc_count),
        )

    console.print(table)


if __name__ == "__main__":
    app()
