"""CLI for Ironclad CLM."""

import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import typer
from rich.console import Console
from shared.cli_tables import Table

app = typer.Typer(name="ironclad", help="Ironclad CLM CLI for AI agents")
console = Console()


def get_client():
    """Get Ironclad client."""
    from .client import IroncladClient

    return IroncladClient()


def format_date(date_str: str | None) -> str:
    """Format ISO date to readable format."""
    if not date_str:
        return "-"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(date_str)


def prop_value(prop) -> str | None:
    """Extract display value from an Ironclad typed property."""
    if prop is None:
        return None
    if isinstance(prop, str):
        return prop
    if isinstance(prop, dict):
        return str(prop.get("value", ""))
    return str(prop)


def truncate(text: str | None, length: int = 30) -> str:
    """Truncate text for display."""
    if not text:
        return "-"
    text = prop_value(text) or "-"
    if len(text) > length:
        return text[:length] + "..."
    return text


# ============== Workflows ==============


@app.command()
def workflows(
    status: str = typer.Option(None, "--status", "-s", help="Filter: active/completed/cancelled"),
    template: str = typer.Option(None, "--template", "-t", help="Filter by template ID"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List workflows."""
    client = get_client()
    result = client.workflows(status=status, template_id=template, limit=limit)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No workflows found.[/]")
        raise typer.Exit()

    table = Table(title=f"Workflows ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Title", style="white", max_width=35)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Template", max_width=25)
    table.add_column("Created", max_width=18)

    for w in result:
        template_info = w.get("template", {})
        table.add_row(
            truncate(w.get("id"), 15),
            truncate(w.get("title") or w.get("name"), 35),
            w.get("status", "-"),
            truncate(
                template_info.get("name", "-") if isinstance(template_info, dict) else "-", 25
            ),
            format_date(w.get("createdDate") or w.get("createdAt")),
        )

    console.print(table)


@app.command()
def workflow(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get workflow details."""
    client = get_client()
    result = client.workflow(workflow_id)

    if not result:
        console.print(f"[red]Workflow '{workflow_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(
        f"\n[bold cyan]Workflow: {result.get('title') or result.get('name', 'Unknown')}[/]"
    )
    console.print(f"ID: {result.get('id', '-')}")
    console.print(f"Status: [green]{result.get('status', '-')}[/]")
    console.print(f"Ironclad ID: {result.get('ironcladId', '-')}")
    console.print(f"Created: {format_date(result.get('createdDate') or result.get('createdAt'))}")

    template = result.get("template", {})
    if template:
        console.print(f"Template: {template.get('name', '-')}")

    attributes = result.get("attributes", {})
    if attributes:
        console.print("\n[bold]Attributes:[/]")
        for key, value in list(attributes.items())[:10]:
            console.print(f"  {key}: {truncate(str(value), 50)}")
        if len(attributes) > 10:
            console.print(f"  ... and {len(attributes) - 10} more")


@app.command()
def signers(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List signers for a workflow."""
    client = get_client()
    result = client.workflow_signers(workflow_id)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No signers found.[/]")
        raise typer.Exit()

    table = Table(title=f"Signers ({len(result)})")
    table.add_column("Name", style="white", max_width=25)
    table.add_column("Email", max_width=30)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Signed At", max_width=18)

    for s in result:
        table.add_row(
            s.get("displayName") or s.get("name", "-"),
            s.get("email", "-"),
            s.get("status", "-"),
            format_date(s.get("signedDate") or s.get("signedAt")),
        )

    console.print(table)


@app.command()
def approvals(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List approvals for a workflow."""
    client = get_client()
    result = client.workflow_approvals(workflow_id)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No approvals found.[/]")
        raise typer.Exit()

    table = Table(title=f"Approvals ({len(result)})")
    table.add_column("Approver", style="white", max_width=25)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Decision", max_width=15)
    table.add_column("Date", max_width=18)

    for a in result:
        approver = a.get("approver", {})
        table.add_row(
            approver.get("displayName") or approver.get("email", "-"),
            a.get("status", "-"),
            a.get("decision", "-"),
            format_date(a.get("completedDate") or a.get("decidedAt")),
        )

    console.print(table)


# ============== Workflow Schemas (Templates) ==============


@app.command()
def schemas(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List workflow schemas (templates)."""
    client = get_client()
    result = client.schemas()

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No schemas found.[/]")
        raise typer.Exit()

    result = result[:limit]

    table = Table(title=f"Workflow Schemas ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=40)
    table.add_column("Published", style="green", max_width=10)

    for s in result:
        table.add_row(
            truncate(s.get("id"), 15),
            truncate(s.get("name"), 40),
            "Yes" if s.get("isPublished") else "No",
        )

    console.print(table)


@app.command()
def schema(
    schema_id: str = typer.Argument(..., help="Schema ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get workflow schema details."""
    client = get_client()
    result = client.schema(schema_id)

    if not result:
        console.print(f"[red]Schema '{schema_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(f"\n[bold cyan]Schema: {result.get('name', 'Unknown')}[/]")
    console.print(f"ID: {result.get('id', '-')}")
    console.print(f"Published: {'Yes' if result.get('isPublished') else 'No'}")

    attributes = result.get("attributes", [])
    if attributes:
        console.print(f"\n[bold]Attributes ({len(attributes)}):[/]")
        for attr in attributes[:15]:
            required = "[red]*[/]" if attr.get("isRequired") else ""
            console.print(
                f"  {attr.get('displayName', attr.get('id', '-'))}{required} ({attr.get('type', '-')})"
            )
        if len(attributes) > 15:
            console.print(f"  ... and {len(attributes) - 15} more")


# ============== Records ==============


@app.command()
def records(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List records (completed contracts)."""
    client = get_client()
    result = client.records(limit=limit)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No records found.[/]")
        raise typer.Exit()

    table = Table(title=f"Records ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=35)
    table.add_column("Counterparty", max_width=25)
    table.add_column("Created", max_width=18)

    for r in result:
        props = r.get("properties", {})
        table.add_row(
            truncate(r.get("id"), 15),
            truncate(r.get("name") or props.get("title") or props.get("contractTitle"), 35),
            truncate(props.get("counterpartyName") or props.get("counterparty"), 25),
            format_date(r.get("createdDate") or r.get("createdAt")),
        )

    console.print(table)


@app.command()
def record(
    record_id: str = typer.Argument(..., help="Record ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get record details."""
    client = get_client()
    result = client.record(record_id)

    if not result:
        console.print(f"[red]Record '{record_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(f"\n[bold cyan]Record: {result.get('name', 'Unknown')}[/]")
    console.print(f"ID: {result.get('id', '-')}")
    console.print(f"Ironclad ID: {result.get('ironcladId', '-')}")
    console.print(f"Created: {format_date(result.get('createdDate') or result.get('createdAt'))}")

    props = result.get("properties", {})
    if props:
        console.print("\n[bold]Properties:[/]")
        for key, value in list(props.items())[:15]:
            console.print(f"  {key}: {truncate(str(value), 50)}")
        if len(props) > 15:
            console.print(f"  ... and {len(props) - 15} more")


# ============== Entities ==============


@app.command()
def entities(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List entities."""
    client = get_client()
    result = client.entities(limit=limit)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No entities found.[/]")
        raise typer.Exit()

    table = Table(title=f"Entities ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=35)
    table.add_column("Type", max_width=20)

    for e in result:
        table.add_row(
            truncate(e.get("id"), 15),
            truncate(e.get("name") or e.get("displayName"), 35),
            e.get("type", "-"),
        )

    console.print(table)


@app.command()
def entity(
    entity_id: str = typer.Argument(..., help="Entity ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get entity details."""
    client = get_client()
    result = client.entity(entity_id)

    if not result:
        console.print(f"[red]Entity '{entity_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(
        f"\n[bold cyan]Entity: {result.get('name') or result.get('displayName', 'Unknown')}[/]"
    )
    console.print(f"ID: {result.get('id', '-')}")
    console.print(f"Type: {result.get('type', '-')}")

    props = result.get("properties", {})
    if props:
        console.print("\n[bold]Properties:[/]")
        for key, value in list(props.items())[:10]:
            console.print(f"  {key}: {truncate(str(value), 50)}")


# ============== Webhooks ==============


@app.command()
def webhooks(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List webhooks."""
    client = get_client()
    result = client.webhooks()

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No webhooks found.[/]")
        raise typer.Exit()

    table = Table(title=f"Webhooks ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Target URL", style="white", max_width=40)
    table.add_column("Events", max_width=30)
    table.add_column("Active", style="green", max_width=8)

    for w in result:
        events = w.get("events", [])
        events_str = ", ".join(events[:3])
        if len(events) > 3:
            events_str += f" +{len(events) - 3}"

        table.add_row(
            truncate(w.get("id"), 15),
            truncate(w.get("targetUrl"), 40),
            events_str,
            "Yes" if w.get("isActive") else "No",
        )

    console.print(table)


# ============== Obligations ==============


@app.command()
def obligations(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List obligations."""
    client = get_client()
    result = client.obligations(limit=limit)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No obligations found.[/]")
        raise typer.Exit()

    table = Table(title=f"Obligations ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Title", style="white", max_width=35)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Due Date", max_width=18)

    for o in result:
        table.add_row(
            truncate(o.get("id"), 15),
            truncate(o.get("title") or o.get("name"), 35),
            o.get("status", "-"),
            format_date(o.get("dueDate")),
        )

    console.print(table)


if __name__ == "__main__":
    app()
