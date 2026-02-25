"""CLI for FalconX trading API."""

import json
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from shared.cli_tables import Table

from .client import AccountType, FalconXClient

load_dotenv()

app = typer.Typer(name="falconx", help="FalconX trading CLI for AI agents")
console = Console()

AccountOption = Annotated[
    AccountType,
    typer.Option("--account", "-a", help="Account: p1 (Paradigm One) or pf (Paradigm Fund)"),
]
JsonOption = Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")]


def get_client(account: AccountType = "p1") -> FalconXClient:
    return FalconXClient(account=account)


def format_number(value: float, decimals: int = 2) -> str:
    """Format numbers with commas."""
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.{decimals}f}M"
    elif abs(value) >= 1e3:
        return f"{value:,.{decimals}f}"
    return f"{value:.{decimals}f}"


@app.command()
def quote(
    base: str = typer.Argument(..., help="Base token (e.g., BTC)"),
    quote_token: str = typer.Argument(..., help="Quote token (e.g., USD)"),
    quantity: float = typer.Argument(..., help="Quantity to trade"),
    side: str = typer.Option("buy", "--side", "-s", help="Trade side: buy or sell"),
    account: AccountOption = "p1",
    json_output: JsonOption = False,
):
    """Get a quote for a trade."""
    client = get_client(account)
    data = client.get_quote(base, quote_token, quantity, side)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold cyan]Quote for {side.upper()} {quantity} {base.upper()}[/]\n")

    quote_id = data.get("fx_quote_id", data.get("quote_id", "N/A"))
    price = data.get("price", data.get("buy_price") or data.get("sell_price", "N/A"))
    total = data.get("total", data.get("gross_amount", "N/A"))
    expires = data.get("expiry_time", data.get("valid_until", "N/A"))

    console.print(f"[bold]Quote ID:[/] {quote_id}")
    console.print(f"[bold]Price:[/] [yellow]{price}[/] {quote_token.upper()}")
    console.print(f"[bold]Total:[/] [green]{total}[/] {quote_token.upper()}")
    console.print(f"[bold]Expires:[/] {expires}")


@app.command()
def execute(
    quote_id: str = typer.Argument(..., help="Quote ID to execute"),
    account: AccountOption = "p1",
    json_output: JsonOption = False,
):
    """Execute a previously obtained quote."""
    client = get_client(account)
    data = client.execute_quote(quote_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    status = data.get("status", "unknown")
    trade_id = data.get("trade_id", data.get("fx_trade_id", "N/A"))

    if status.lower() in ["success", "executed", "filled"]:
        console.print("[green]✓ Trade executed successfully[/]")
    else:
        console.print(f"[yellow]Trade status: {status}[/]")

    console.print(f"[bold]Trade ID:[/] {trade_id}")


@app.command()
def balances(
    account: AccountOption = "p1",
    json_output: JsonOption = False,
):
    """Get account balances."""
    client = get_client(account)
    data = client.get_balances()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    # Handle both list and dict responses
    if isinstance(data, list):
        balances_list = data
    else:
        balances_list = data.get("balances", data.get("data", []))
        if not balances_list:
            balances_list = [{"token": k, "balance": v} for k, v in data.items() if k != "status"]

    if not balances_list:
        console.print("[yellow]No balances found.[/]")
        return

    table = Table(title=f"Account Balances ({account.upper()})")
    table.add_column("Token", style="cyan")
    table.add_column("Balance", style="yellow", justify="right")
    table.add_column("Available", style="green", justify="right")

    for bal in balances_list:
        token = bal.get("token", bal.get("currency", bal.get("asset", "")))
        balance = bal.get("balance", bal.get("total", 0))
        available = bal.get("available", bal.get("available_balance", balance))
        if float(balance) > 0:
            table.add_row(token, format_number(float(balance)), format_number(float(available)))

    console.print(table)


@app.command()
def trades(
    days: int = typer.Option(30, "--days", "-d", help="Days of history (max 31)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results to display"),
    account: AccountOption = "p1",
    json_output: JsonOption = False,
):
    """List trade history (executed quotes)."""
    client = get_client(account)
    data = client.list_trades(days=days)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No trades found.[/]")
        return

    table = Table(title="Trade History")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Pair", style="cyan")
    table.add_column("Side", style="white")
    table.add_column("Quantity", style="yellow", justify="right")
    table.add_column("Price", style="green", justify="right")
    table.add_column("Status", style="white")

    for trade in data[:limit]:
        trade_id = str(trade.get("trade_id", trade.get("fx_trade_id", "")))[:12]
        pair = trade.get("token_pair", {})
        if isinstance(pair, dict):
            pair_str = f"{pair.get('base_token', '')}/{pair.get('quote_token', '')}"
        else:
            pair_str = str(pair)
        side = trade.get("side", "")
        qty = trade.get("quantity", trade.get("filled_quantity", {}))
        if isinstance(qty, dict):
            qty_str = f"{qty.get('value', '')} {qty.get('token', '')}"
        else:
            qty_str = str(qty)
        price = trade.get("price", "")
        status = trade.get("status", "")
        table.add_row(trade_id, pair_str, side, qty_str, str(price), status)

    console.print(table)


@app.command()
def trade(
    trade_id: str = typer.Argument(..., help="Trade ID"),
    account: AccountOption = "p1",
    json_output: JsonOption = False,
):
    """Get details for a specific trade."""
    client = get_client(account)
    data = client.get_trade(trade_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold cyan]Trade {trade_id}[/]\n")
    for key, value in data.items():
        if isinstance(value, dict):
            console.print(f"[bold]{key}:[/]")
            for k, v in value.items():
                console.print(f"  {k}: {v}")
        else:
            console.print(f"[bold]{key}:[/] {value}")


@app.command()
def pairs(
    account: AccountOption = "p1",
    json_output: JsonOption = False,
):
    """List available trading pairs."""
    client = get_client(account)
    data = client.list_pairs()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No pairs found.[/]")
        return

    table = Table(title="Trading Pairs")
    table.add_column("Base", style="cyan")
    table.add_column("Quote", style="green")
    table.add_column("Status", style="yellow")

    for pair in data:
        if isinstance(pair, dict):
            base = pair.get("base_token", pair.get("base", ""))
            quote_tok = pair.get("quote_token", pair.get("quote", ""))
            status = pair.get("status", pair.get("is_active", ""))
            if status is True:
                status = "active"
            elif status is False:
                status = "inactive"
        else:
            parts = str(pair).split("/")
            base = parts[0] if parts else str(pair)
            quote_tok = parts[1] if len(parts) > 1 else ""
            status = ""
        table.add_row(base, quote_tok, str(status))

    console.print(table)


@app.command()
def raw(
    endpoint: str = typer.Argument(..., help="API endpoint (e.g., /v1/balances)"),
    method: str = typer.Option("GET", "--method", "-X", help="HTTP method"),
    body: str = typer.Option(None, "--data", "-d", help="Request body as JSON"),
    account: AccountOption = "p1",
):
    """Make a raw API call."""
    client = get_client(account)

    body_dict = None
    if body:
        body_dict = json.loads(body)

    data = client.raw_request(method, endpoint, body=body_dict)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    app()
