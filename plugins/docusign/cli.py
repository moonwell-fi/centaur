"""CLI for DocuSign eSignature."""

from dotenv import load_dotenv

load_dotenv()

import json
import sys
from datetime import datetime

import typer
from rich.console import Console
from shared.cli_tables import Table

app = typer.Typer(name="docusign", help="DocuSign eSignature CLI for AI agents")
console = Console()


def get_client():
    """Get DocuSign client with env loading."""
    from .client import DocuSignClient

    return DocuSignClient()


def format_date(date_str: str | None) -> str:
    """Format ISO date to readable format."""
    if not date_str:
        return "-"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(date_str)


def truncate(text: str | None, length: int = 30) -> str:
    """Truncate text for display."""
    if not text:
        return "-"
    if len(text) > length:
        return text[:length] + "..."
    return text


# ============== Account ==============


@app.command()
def account(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get account information."""
    client = get_client()
    result = client.account_info()

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No account info available.[/]")
        raise typer.Exit()

    console.print("\n[bold cyan]DocuSign Account[/]")

    if "name" in result:
        console.print(f"Name: {result.get('name', '-')}")
    if "email" in result:
        console.print(f"Email: {result.get('email', '-')}")
    if "sub" in result:
        console.print(f"User ID: {result.get('sub', '-')}")

    accounts = result.get("accounts", [])
    if accounts:
        console.print("\n[bold]Accounts:[/]")
        for acc in accounts:
            is_default = "[green]✓[/] " if acc.get("is_default") else "  "
            console.print(
                f"  {is_default}{acc.get('account_name', '-')} ({acc.get('account_id', '-')})"
            )


# ============== Envelopes ==============


@app.command()
def envelopes(
    status: str = typer.Option(
        None, "--status", "-s", help="Filter: sent/delivered/completed/declined/voided"
    ),
    from_date: str = typer.Option(None, "--from-date", "-f", help="From date (YYYY-MM-DD)"),
    to_date: str = typer.Option(None, "--to-date", "-t", help="To date (YYYY-MM-DD)"),
    search: str = typer.Option(None, "--search", "-q", help="Search text"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List envelopes."""
    client = get_client()
    result = client.envelopes(
        status=status, from_date=from_date, to_date=to_date, search=search, limit=limit
    )

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No envelopes found.[/]")
        raise typer.Exit()

    table = Table(title=f"Envelopes ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Subject", style="white", max_width=35)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Sent", max_width=18)

    for e in result:
        table.add_row(
            truncate(e.get("envelopeId"), 15),
            truncate(e.get("emailSubject"), 35),
            e.get("status", "-"),
            format_date(e.get("sentDateTime") or e.get("createdDateTime")),
        )

    console.print(table)


@app.command()
def envelope(
    envelope_id: str = typer.Argument(..., help="Envelope ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get envelope details."""
    client = get_client()
    result = client.envelope(envelope_id)

    if not result:
        console.print(f"[red]Envelope '{envelope_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(f"\n[bold cyan]Envelope: {result.get('emailSubject', 'Unknown')}[/]")
    console.print(f"ID: {result.get('envelopeId', '-')}")
    console.print(f"Status: [green]{result.get('status', '-')}[/]")
    console.print(f"Created: {format_date(result.get('createdDateTime'))}")
    console.print(f"Sent: {format_date(result.get('sentDateTime'))}")
    console.print(f"Completed: {format_date(result.get('completedDateTime'))}")

    if result.get("voidedReason"):
        console.print(f"Voided Reason: {result.get('voidedReason')}")


@app.command()
def recipients(
    envelope_id: str = typer.Argument(..., help="Envelope ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List recipients for an envelope."""
    client = get_client()
    result = client.envelope_recipients(envelope_id)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No recipients found.[/]")
        raise typer.Exit()

    table = Table(title="Recipients")
    table.add_column("Name", style="white", max_width=25)
    table.add_column("Email", max_width=30)
    table.add_column("Role", max_width=15)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Signed", max_width=18)

    for recipient_type in ["signers", "carbonCopies", "certifiedDeliveries", "inPersonSigners"]:
        for r in result.get(recipient_type, []):
            table.add_row(
                r.get("name", "-"),
                r.get("email", "-"),
                r.get("roleName") or recipient_type[:-1],
                r.get("status", "-"),
                format_date(r.get("signedDateTime")),
            )

    console.print(table)


@app.command()
def documents(
    envelope_id: str = typer.Argument(..., help="Envelope ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List documents in an envelope."""
    client = get_client()
    result = client.envelope_documents(envelope_id)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No documents found.[/]")
        raise typer.Exit()

    table = Table(title=f"Documents ({len(result)})")
    table.add_column("ID", style="cyan", max_width=10)
    table.add_column("Name", style="white", max_width=40)
    table.add_column("Type", max_width=15)
    table.add_column("Pages", max_width=8)

    for d in result:
        table.add_row(
            d.get("documentId", "-"),
            truncate(d.get("name"), 40),
            d.get("type", "-"),
            str(d.get("pages", "-")),
        )

    console.print(table)


@app.command("resend")
def resend_envelope(
    envelope_id: str = typer.Argument(..., help="Envelope ID"),
):
    """Resend envelope notifications to recipients."""
    client = get_client()
    client.resend_envelope(envelope_id)
    console.print(f"[green]Envelope '{envelope_id}' notifications resent.[/]")


@app.command("void")
def void_envelope(
    envelope_id: str = typer.Argument(..., help="Envelope ID"),
    reason: str = typer.Option(..., "--reason", "-r", help="Reason for voiding"),
):
    """Void an envelope."""
    client = get_client()
    client.void_envelope(envelope_id, reason)
    console.print(f"[green]Envelope '{envelope_id}' voided.[/]")


# ============== Templates ==============


@app.command()
def templates(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List templates."""
    client = get_client()
    result = client.templates(limit=limit)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No templates found.[/]")
        raise typer.Exit()

    table = Table(title=f"Templates ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=40)
    table.add_column("Owner", max_width=20)
    table.add_column("Modified", max_width=18)

    for t in result:
        owner = t.get("owner", {})
        table.add_row(
            truncate(t.get("templateId"), 15),
            truncate(t.get("name"), 40),
            owner.get("userName", "-") if isinstance(owner, dict) else "-",
            format_date(t.get("lastModified")),
        )

    console.print(table)


@app.command()
def template(
    template_id: str = typer.Argument(..., help="Template ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get template details."""
    client = get_client()
    result = client.template(template_id)

    if not result:
        console.print(f"[red]Template '{template_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(f"\n[bold cyan]Template: {result.get('name', 'Unknown')}[/]")
    console.print(f"ID: {result.get('templateId', '-')}")
    console.print(f"Description: {result.get('description') or '-'}")
    console.print(f"Modified: {format_date(result.get('lastModified'))}")

    recipients = result.get("recipients", {})
    signers = recipients.get("signers", [])
    if signers:
        console.print(f"\n[bold]Signer Roles ({len(signers)}):[/]")
        for s in signers:
            console.print(
                f"  - {s.get('roleName', 'Signer')} (Order: {s.get('routingOrder', '-')})"
            )


# ============== Users ==============


@app.command()
def users(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List users in the account."""
    client = get_client()
    result = client.users(limit=limit)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No users found.[/]")
        raise typer.Exit()

    table = Table(title=f"Users ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=25)
    table.add_column("Email", max_width=30)
    table.add_column("Status", style="green", max_width=12)

    for u in result:
        table.add_row(
            truncate(u.get("userId"), 15),
            u.get("userName", "-"),
            u.get("email", "-"),
            u.get("userStatus", "-"),
        )

    console.print(table)


@app.command()
def user(
    user_id: str = typer.Argument(..., help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get user details."""
    client = get_client()
    result = client.user(user_id)

    if not result:
        console.print(f"[red]User '{user_id}' not found.[/]")
        raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    console.print(f"\n[bold cyan]User: {result.get('userName', 'Unknown')}[/]")
    console.print(f"ID: {result.get('userId', '-')}")
    console.print(f"Email: {result.get('email', '-')}")
    console.print(f"Status: [green]{result.get('userStatus', '-')}[/]")
    console.print(f"Title: {result.get('jobTitle') or '-'}")
    console.print(f"Company: {result.get('company') or '-'}")


# ============== Folders ==============


@app.command()
def folders(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List folders."""
    client = get_client()
    result = client.folders()

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No folders found.[/]")
        raise typer.Exit()

    table = Table(title=f"Folders ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=30)
    table.add_column("Type", max_width=15)
    table.add_column("Items", max_width=8)

    for f in result:
        table.add_row(
            truncate(f.get("folderId"), 15),
            f.get("name", "-"),
            f.get("type", "-"),
            str(f.get("itemCount", "-")),
        )

    console.print(table)


# ============== Signing Groups ==============


@app.command("signing-groups")
def signing_groups(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List signing groups."""
    client = get_client()
    result = client.signing_groups()

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No signing groups found.[/]")
        raise typer.Exit()

    table = Table(title=f"Signing Groups ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=30)
    table.add_column("Email", max_width=30)
    table.add_column("Members", max_width=8)

    for g in result:
        users = g.get("users", [])
        table.add_row(
            truncate(g.get("signingGroupId"), 15),
            g.get("groupName", "-"),
            g.get("groupEmail", "-"),
            str(len(users)),
        )

    console.print(table)


# ============== Brands ==============


@app.command()
def brands(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List brands."""
    client = get_client()
    result = client.brands()

    if json_output:
        print(json.dumps(result, indent=2, default=str), file=sys.stdout)
        raise typer.Exit()

    if not result:
        console.print("[yellow]No brands found.[/]")
        raise typer.Exit()

    table = Table(title=f"Brands ({len(result)})")
    table.add_column("ID", style="cyan", max_width=15)
    table.add_column("Name", style="white", max_width=30)
    table.add_column("Default", style="green", max_width=10)

    for b in result:
        is_default = "Yes" if b.get("isDefault") else "No"
        table.add_row(
            truncate(b.get("brandId"), 15),
            b.get("brandName", "-"),
            is_default,
        )

    console.print(table)


if __name__ == "__main__":
    app()
