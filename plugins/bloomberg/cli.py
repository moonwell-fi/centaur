"""CLI for Bloomberg Data License REST API."""

from dotenv import load_dotenv

load_dotenv()

import json

import typer
from shared.cli_tables import Table
from rich.console import Console

app = typer.Typer(name="bloomberg", help="Bloomberg Data License REST API CLI")
console = Console()


def get_client(beta: bool = False):
    from .client import BloombergClient

    return BloombergClient(use_beta=beta)


@app.command()
def catalog(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List available data catalogs."""
    client = get_client(beta)
    try:
        data = client.get_catalog()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        if isinstance(data, dict) and "contains" in data:
            items = data["contains"]
        elif isinstance(data, list):
            items = data
        else:
            items = [data]

        table = Table(title="Bloomberg Data Catalogs")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Description", style="dim", max_width=50)

        for item in items:
            table.add_row(
                item.get("identifier", "N/A"),
                item.get("title", "N/A"),
                item.get("description", "")[:50] if item.get("description") else "",
            )

        console.print(table)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def datasets(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results to show"),
):
    """List datasets."""
    client = get_client(beta)
    try:
        data = client.get_datasets()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        items = data.get("contains", data) if isinstance(data, dict) else data
        items = items[:limit] if isinstance(items, list) else [items]

        table = Table(title="Datasets")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="white", max_width=40)
        table.add_column("Type", style="yellow")

        for item in items:
            table.add_row(
                item.get("identifier", "N/A"),
                item.get("title", item.get("name", "N/A")),
                item.get("@type", "N/A"),
            )

        console.print(table)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def fields(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results to show"),
    query: str = typer.Option(None, "--query", "-q", help="Filter fields by name"),
):
    """List available fields."""
    client = get_client(beta)
    try:
        data = client.get_fields()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        items = data.get("contains", data) if isinstance(data, dict) else data
        if query and isinstance(items, list):
            items = [
                f
                for f in items
                if query.lower() in str(f.get("mnemonic", "")).lower()
                or query.lower() in str(f.get("description", "")).lower()
            ]
        items = items[:limit] if isinstance(items, list) else [items]

        table = Table(title="Bloomberg Fields")
        table.add_column("Mnemonic", style="cyan")
        table.add_column("Description", style="white", max_width=50)
        table.add_column("Type", style="yellow")

        for item in items:
            table.add_row(
                item.get("mnemonic", item.get("identifier", "N/A")),
                (item.get("description", "") or "")[:50],
                item.get("dataType", item.get("@type", "N/A")),
            )

        console.print(table)
        if len(items) >= limit:
            console.print(f"[dim]Showing first {limit} results. Use --limit to see more.[/]")
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def universes(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List user universes."""
    client = get_client(beta)
    try:
        data = client.get_universes()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        items = data.get("contains", data) if isinstance(data, dict) else data
        if not items:
            console.print("[yellow]No universes found[/]")
            return

        table = Table(title="User Universes")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Securities", style="yellow", justify="right")

        for item in items if isinstance(items, list) else [items]:
            contains = item.get("contains", [])
            table.add_row(
                item.get("identifier", "N/A"),
                item.get("title", "N/A"),
                str(len(contains)) if isinstance(contains, list) else "N/A",
            )

        console.print(table)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command("create-universe")
def create_universe(
    name: str = typer.Argument(..., help="Universe name/identifier"),
    tickers: str = typer.Argument(
        ..., help="Comma-separated tickers (e.g., AAPL US Equity,MSFT US Equity)"
    ),
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a universe of securities."""
    client = get_client(beta)
    try:
        ticker_list = [t.strip() for t in tickers.split(",") if t.strip()]
        data = client.create_universe(name, ticker_list)
        if json_output:
            print(json.dumps(data, indent=2))
        else:
            console.print(f"[green]Created universe '{name}' with {len(ticker_list)} securities[/]")
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def requests(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List data requests."""
    client = get_client(beta)
    try:
        data = client.get_requests()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        items = data.get("contains", data) if isinstance(data, dict) else data
        if not items:
            console.print("[yellow]No requests found[/]")
            return

        table = Table(title="Data Requests")
        table.add_column("ID", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="dim")

        for item in items if isinstance(items, list) else [items]:
            status = item.get("status", item.get("@type", "N/A"))
            table.add_row(
                item.get("identifier", "N/A"),
                status,
                item.get("created", item.get("dateCreated", "N/A")),
            )

        console.print(table)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command("create-request")
def create_request(
    universe: str = typer.Argument(..., help="Universe ID"),
    fields_str: str = typer.Argument(
        ..., help="Comma-separated fields (e.g., PX_LAST,PX_OPEN,PX_HIGH)"
    ),
    request_id: str = typer.Option(None, "--id", help="Custom request ID"),
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a data request for a universe."""
    client = get_client(beta)
    try:
        field_list = [f.strip() for f in fields_str.split(",") if f.strip()]
        data = client.create_request(universe, field_list, request_id)
        if json_output:
            print(json.dumps(data, indent=2))
        else:
            console.print(
                f"[green]Created request for universe '{universe}' with {len(field_list)} fields[/]"
            )
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command("request-status")
def request_status(
    request_id: str = typer.Argument(..., help="Request ID"),
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Get status of a data request."""
    client = get_client(beta)
    try:
        data = client.get_request_status(request_id)
        if json_output:
            print(json.dumps(data, indent=2))
        else:
            console.print(f"[bold]Request:[/] {request_id}")
            console.print(f"[bold]Status:[/] {data.get('status', data.get('@type', 'N/A'))}")
            if data.get("percentComplete"):
                console.print(f"[bold]Progress:[/] {data.get('percentComplete')}%")
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def distributions(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results to show"),
):
    """List available distributions (completed outputs)."""
    client = get_client(beta)
    try:
        data = client.get_distributions()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        items = data.get("contains", data) if isinstance(data, dict) else data
        items = items[:limit] if isinstance(items, list) else [items]

        if not items:
            console.print("[yellow]No distributions found[/]")
            return

        table = Table(title="Distributions")
        table.add_column("ID", style="cyan")
        table.add_column("Dataset", style="white")
        table.add_column("Date", style="yellow")
        table.add_column("Files", style="dim", justify="right")

        for item in items:
            files = item.get("files", item.get("contains", []))
            table.add_row(
                item.get("identifier", "N/A"),
                item.get("dataset", item.get("title", "N/A")),
                item.get("snapshotDate", item.get("date", "N/A")),
                str(len(files)) if isinstance(files, list) else "N/A",
            )

        console.print(table)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def schedules(
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List scheduled jobs."""
    client = get_client(beta)
    try:
        data = client.get_schedules()
        if json_output:
            print(json.dumps(data, indent=2))
            return

        items = data.get("contains", data) if isinstance(data, dict) else data
        if not items:
            console.print("[yellow]No schedules found[/]")
            return

        table = Table(title="Schedules")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="white")
        table.add_column("Status", style="yellow")
        table.add_column("Frequency", style="dim")

        for item in items if isinstance(items, list) else [items]:
            table.add_row(
                item.get("identifier", "N/A"),
                item.get("title", item.get("name", "N/A")),
                item.get("status", "N/A"),
                item.get("frequency", item.get("schedule", "N/A")),
            )

        console.print(table)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def download(
    distribution_id: str = typer.Argument(..., help="Distribution ID"),
    filename: str = typer.Argument(..., help="Filename to download"),
    output: str = typer.Option(
        None, "--output", "-o", help="Output file path (default: same as filename)"
    ),
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
):
    """Download a distribution file."""
    client = get_client(beta)
    try:
        content = client.download_distribution(distribution_id, filename)
        output_path = output or filename
        with open(output_path, "wb") as f:
            f.write(content)
        console.print(f"[green]Downloaded {len(content)} bytes to {output_path}[/]")
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def raw(
    method: str = typer.Argument(..., help="HTTP method (GET, POST, etc.)"),
    path: str = typer.Argument(..., help="API path (e.g., /eap/catalogs/)"),
    body: str = typer.Option(None, "--body", "-d", help="JSON body for POST/PUT"),
    beta: bool = typer.Option(False, "--beta", "-b", help="Use beta environment"),
):
    """Make a raw API request."""
    client = get_client(beta)
    try:
        json_body = json.loads(body) if body else None
        data = client._request(method, path, json_body=json_body)
        print(json.dumps(data, indent=2))
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON body: {e}[/]")
        raise typer.Exit(1)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    app()
