"""CLI for Coinbase Prime API."""

import json
import os
from collections import defaultdict

import typer
from dotenv import load_dotenv
from rich.console import Console
from shared.cli_tables import Table

load_dotenv()

app = typer.Typer(name="coinbase", help="Coinbase Prime CLI for custody, staking, and portfolios")
console = Console()

# Portfolio name mappings to env var suffixes
PORTFOLIO_MAP = {
    # Default/main portfolio
    "default": "",
    "pf": "",
    "fund": "",
    # Sub-portfolios under pf
    "sp12": "_SP12",
    "sp15": "_SP15",
    "sp28": "_SP28",
    "sp7": "_SP7",
    "sp8": "_SP8",
    # Operations portfolios
    "po": "_PO",
    "ops": "_PO",
    "operations": "_PO",
    "po_sp14": "_PO_SP14",
    "po_sp2": "_PO_SP2",
    "po_sp4": "_PO_SP4",
    # Scrip
    "scrip": "_SCRIP",
}

# All portfolio suffixes for --all queries
ALL_PORTFOLIOS = [
    "",
    "_SP12",
    "_SP15",
    "_SP28",
    "_SP7",
    "_SP8",
    "_PO",
    "_PO_SP14",
    "_PO_SP2",
    "_PO_SP4",
    "_SCRIP",
]


def get_credentials_for_portfolio(suffix: str = "") -> tuple[str, str, str, str] | None:
    """Get credentials for a specific portfolio suffix."""
    api_key = os.getenv(f"COINBASE_API_KEY{suffix}")
    api_secret = os.getenv(f"COINBASE_API_SECRET{suffix}")
    passphrase = os.getenv(f"COINBASE_API_PASSPHRASE{suffix}")
    portfolio_id = os.getenv(f"COINBASE_PORTFOLIO_ID{suffix}")

    if api_key and api_secret and passphrase and portfolio_id:
        return api_key, api_secret, passphrase, portfolio_id
    return None


def resolve_portfolio(name: str | None) -> str:
    """Resolve portfolio name to env var suffix."""
    if name is None:
        return ""

    name_lower = name.lower().replace("-", "_")
    if name_lower in PORTFOLIO_MAP:
        return PORTFOLIO_MAP[name_lower]

    # Check if it's a raw portfolio ID (UUID format)
    if len(name) == 36 and name.count("-") == 4:
        return name  # Return as-is, will be used directly

    raise typer.BadParameter(
        f"Unknown portfolio '{name}'. Valid names: {', '.join(sorted(set(PORTFOLIO_MAP.keys())))}"
    )


def get_portfolio_id(portfolio: str | None = None) -> str:
    """Get portfolio ID from name or env."""
    suffix = resolve_portfolio(portfolio)

    # If suffix looks like a UUID, return it directly
    if len(suffix) == 36 and suffix.count("-") == 4:
        return suffix

    portfolio_id = os.getenv(f"COINBASE_PORTFOLIO_ID{suffix}")
    if not portfolio_id:
        raise typer.BadParameter(
            f"COINBASE_PORTFOLIO_ID{suffix} not set. Run 'coinbase portfolios' to see available portfolios."
        )
    return portfolio_id


def set_credentials_for_suffix(suffix: str) -> None:
    """Temporarily set credentials for a portfolio suffix."""
    creds = get_credentials_for_portfolio(suffix)
    if creds:
        os.environ["COINBASE_API_KEY"] = creds[0]
        os.environ["COINBASE_API_SECRET"] = creds[1]
        os.environ["COINBASE_API_PASSPHRASE"] = creds[2]
        os.environ["COINBASE_PORTFOLIO_ID"] = creds[3]


def format_amount(value: str | float | None, decimals: int = 8) -> str:
    """Format crypto amount."""
    if value is None:
        return "0"
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    if value == 0:
        return "0"
    if value >= 1_000_000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:,.6f}"
    else:
        return f"{value:.8f}"


def format_usd(value: float | str | None) -> str:
    """Format USD amount."""
    if value is None:
        return "$0.00"
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    elif value >= 1e6:
        return f"${value / 1e6:.2f}M"
    elif value >= 1e3:
        return f"${value / 1e3:.2f}K"
    return f"${value:,.2f}"


def suffix_to_name(suffix: str) -> str:
    """Convert env var suffix to friendly name."""
    if suffix == "":
        return "pf"
    return suffix.lstrip("_").lower()


def print_markdown_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a markdown-formatted table."""
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        print("| " + " | ".join(str(cell) for cell in row) + " |")


@app.command()
def portfolios(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List all configured portfolios."""
    results = []

    for suffix in ALL_PORTFOLIOS:
        creds = get_credentials_for_portfolio(suffix)
        if creds:
            name = suffix_to_name(suffix)
            results.append(
                {
                    "name": name,
                    "suffix": suffix,
                    "portfolio_id": creds[3],
                }
            )

    if json_output:
        print(json.dumps(results, indent=2))
        return

    if not results:
        console.print("[yellow]No portfolios configured.[/]")
        raise typer.Exit()

    if markdown:
        rows = [[r["name"], r["portfolio_id"][:20] + "..."] for r in results]
        print_markdown_table(["Name", "Portfolio ID"], rows)
        return

    table = Table(title=f"Configured Portfolios ({len(results)})")
    table.add_column("Name", style="cyan")
    table.add_column("Portfolio ID", style="dim")

    for r in results:
        table.add_row(r["name"], r["portfolio_id"])

    console.print(table)
    console.print("\n[dim]Use -f NAME to query a specific portfolio, or --all to aggregate all.[/]")


@app.command()
def balances(
    fund: str = typer.Option(
        None, "--fund", "-f", help="Portfolio name (pf, po, sp7, scrip, etc.)"
    ),
    all_funds: bool = typer.Option(False, "--all", "-a", help="Aggregate across all portfolios"),
    symbols: str = typer.Option(
        None, "--symbols", "-s", help="Filter by symbols (comma-separated)"
    ),
    nonzero: bool = typer.Option(False, "--nonzero", "-nz", help="Only show non-zero balances"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """Get portfolio balances.

    Examples:
        coinbase balances                    # Default portfolio (pf)
        coinbase balances -f po              # Operations portfolio
        coinbase balances -f sp7             # SP7 sub-portfolio
        coinbase balances --all              # All portfolios aggregated
        coinbase balances --all --nonzero    # All non-zero across all portfolios
    """
    from .client import CoinbasePrimeClient

    symbol_list = symbols.split(",") if symbols else None

    if all_funds:
        # Aggregate across all portfolios
        aggregated: dict[str, dict] = defaultdict(
            lambda: {"amount": 0.0, "holds": 0.0, "fiat_amount": 0.0}
        )

        for suffix in ALL_PORTFOLIOS:
            creds = get_credentials_for_portfolio(suffix)
            if not creds:
                continue

            set_credentials_for_suffix(suffix)
            try:
                client = CoinbasePrimeClient()
                data = client.get_portfolio_balances(creds[3], symbol_list)

                for b in data:
                    symbol = b.get("symbol", "").upper()
                    try:
                        amount = float(b.get("amount", 0) or 0)
                        holds = float(b.get("holds", 0) or 0)
                        fiat = float(b.get("fiat_amount", 0) or 0)
                    except (ValueError, TypeError):
                        continue

                    aggregated[symbol]["amount"] += amount
                    aggregated[symbol]["holds"] += holds
                    aggregated[symbol]["fiat_amount"] += fiat
            except Exception:
                continue

        # Convert to list and sort by USD value
        data = [{"symbol": sym, **vals} for sym, vals in aggregated.items()]
        data.sort(key=lambda x: x.get("fiat_amount", 0), reverse=True)
        title = "Balances (All Portfolios)"
    else:
        suffix = resolve_portfolio(fund)
        if suffix and len(suffix) != 36:  # Not a raw UUID
            set_credentials_for_suffix(suffix)
        pid = get_portfolio_id(fund)
        client = CoinbasePrimeClient()
        data = client.get_portfolio_balances(pid, symbol_list)
        title = f"Balances ({suffix_to_name(suffix) if suffix else 'pf'})"

    # Filter non-zero if requested
    if nonzero:
        data = [b for b in data if float(b.get("amount", 0) or 0) > 0]

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No balances found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for b in data:
            amount = format_amount(b.get("amount"))
            holds = format_amount(b.get("holds"))
            usd = format_usd(b.get("fiat_amount"))
            rows.append([b.get("symbol", "").upper(), amount, holds, usd])
        print_markdown_table(["Symbol", "Amount", "Holds", "USD Value"], rows)
        return

    table = Table(title=f"{title} ({len(data)})")
    table.add_column("Symbol", style="cyan")
    table.add_column("Amount", style="yellow", justify="right")
    table.add_column("Holds", style="dim", justify="right")
    table.add_column("USD Value", style="green", justify="right")

    for b in data:
        amount = format_amount(b.get("amount"))
        holds = format_amount(b.get("holds"))
        usd = format_usd(b.get("fiat_amount"))
        table.add_row(b.get("symbol", "").upper(), amount, holds, usd)

    console.print(table)


@app.command()
def wallets(
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    wallet_type: str = typer.Option(None, "--type", "-t", help="Filter by type (VAULT, TRADING)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List wallets."""
    from .client import CoinbasePrimeClient

    suffix = resolve_portfolio(fund)
    if suffix:
        set_credentials_for_suffix(suffix)
    pid = get_portfolio_id(fund)
    client = CoinbasePrimeClient()
    data = client.list_wallets(pid, wallet_type)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No wallets found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for w in data:
            rows.append(
                [
                    w.get("name", ""),
                    w.get("symbol", ""),
                    w.get("type", ""),
                    w.get("id", "")[:16] + "...",
                ]
            )
        print_markdown_table(["Name", "Symbol", "Type", "ID"], rows)
        return

    table = Table(title=f"Wallets ({len(data)})")
    table.add_column("Name", style="cyan", max_width=25)
    table.add_column("Symbol", style="green")
    table.add_column("Type", style="white")
    table.add_column("ID", style="dim", max_width=20)

    for w in data:
        table.add_row(
            w.get("name", ""),
            w.get("symbol", ""),
            w.get("type", ""),
            w.get("id", "")[:16] + "...",
        )

    console.print(table)


@app.command()
def wallet(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get wallet details."""
    from .client import CoinbasePrimeClient

    suffix = resolve_portfolio(fund)
    if suffix:
        set_credentials_for_suffix(suffix)
    pid = get_portfolio_id(fund)
    client = CoinbasePrimeClient()
    data = client.get_wallet(pid, wallet_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold cyan]{data.get('name', 'Wallet')}[/]")
    console.print(f"[dim]ID: {data.get('id', '')}[/]\n")
    console.print(f"Symbol: [green]{data.get('symbol', '')}[/]")
    console.print(f"Type: {data.get('type', '')}")
    if data.get("address"):
        console.print(f"Address: [cyan]{data.get('address')}[/]")


@app.command()
def transactions(
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    symbols: str = typer.Option(None, "--symbols", "-s", help="Filter by symbols"),
    types: str = typer.Option(None, "--types", "-t", help="Filter by types"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List transactions."""
    from .client import CoinbasePrimeClient

    suffix = resolve_portfolio(fund)
    if suffix:
        set_credentials_for_suffix(suffix)
    pid = get_portfolio_id(fund)
    client = CoinbasePrimeClient()
    symbol_list = symbols.split(",") if symbols else None
    type_list = types.split(",") if types else None
    data = client.list_transactions(pid, symbol_list, type_list, limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No transactions found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for tx in data:
            amount = format_amount(tx.get("amount"))
            rows.append(
                [
                    tx.get("created_at", "")[:10],
                    tx.get("type", ""),
                    tx.get("symbol", ""),
                    amount,
                    tx.get("status", ""),
                ]
            )
        print_markdown_table(["Date", "Type", "Symbol", "Amount", "Status"], rows)
        return

    table = Table(title=f"Transactions ({len(data)})")
    table.add_column("Date", style="dim", max_width=12)
    table.add_column("Type", style="cyan")
    table.add_column("Symbol", style="green")
    table.add_column("Amount", style="yellow", justify="right")
    table.add_column("Status", style="white")

    for tx in data:
        amount = format_amount(tx.get("amount"))
        table.add_row(
            tx.get("created_at", "")[:10],
            tx.get("type", ""),
            tx.get("symbol", ""),
            amount,
            tx.get("status", ""),
        )

    console.print(table)


@app.command()
def staking(
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    all_funds: bool = typer.Option(False, "--all", "-a", help="Aggregate across all portfolios"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List staking positions."""
    from .client import CoinbasePrimeClient

    if all_funds:
        all_data = []
        for suffix in ALL_PORTFOLIOS:
            creds = get_credentials_for_portfolio(suffix)
            if not creds:
                continue
            set_credentials_for_suffix(suffix)
            try:
                client = CoinbasePrimeClient()
                data = client.list_staking_positions(creds[3])
                for pos in data:
                    pos["portfolio"] = suffix_to_name(suffix)
                all_data.extend(data)
            except Exception:
                continue
        data = all_data
        title = "Staking Positions (All Portfolios)"
    else:
        suffix = resolve_portfolio(fund)
        if suffix:
            set_credentials_for_suffix(suffix)
        pid = get_portfolio_id(fund)
        client = CoinbasePrimeClient()
        data = client.list_staking_positions(pid)
        title = "Staking Positions"

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No staking positions found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for pos in data:
            amount = format_amount(pos.get("staked_balance"))
            rewards = format_amount(pos.get("total_rewards"))
            row = [pos.get("symbol", ""), amount, rewards, pos.get("status", "")]
            if all_funds:
                row.insert(0, pos.get("portfolio", ""))
            rows.append(row)
        headers = ["Symbol", "Staked", "Total Rewards", "Status"]
        if all_funds:
            headers.insert(0, "Portfolio")
        print_markdown_table(headers, rows)
        return

    table = Table(title=f"{title} ({len(data)})")
    if all_funds:
        table.add_column("Portfolio", style="blue")
    table.add_column("Symbol", style="cyan")
    table.add_column("Staked", style="yellow", justify="right")
    table.add_column("Total Rewards", style="green", justify="right")
    table.add_column("Status", style="white")

    for pos in data:
        amount = format_amount(pos.get("staked_balance"))
        rewards = format_amount(pos.get("total_rewards"))
        if all_funds:
            table.add_row(
                pos.get("portfolio", ""),
                pos.get("symbol", ""),
                amount,
                rewards,
                pos.get("status", ""),
            )
        else:
            table.add_row(pos.get("symbol", ""), amount, rewards, pos.get("status", ""))

    console.print(table)


@app.command()
def rewards(
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    symbol: str = typer.Option(None, "--symbol", "-s", help="Filter by symbol"),
    start_date: str = typer.Option(None, "--start", help="Start date (YYYY-MM-DD)"),
    end_date: str = typer.Option(None, "--end", help="End date (YYYY-MM-DD)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """Get staking rewards."""
    from .client import CoinbasePrimeClient

    suffix = resolve_portfolio(fund)
    if suffix:
        set_credentials_for_suffix(suffix)
    pid = get_portfolio_id(fund)
    client = CoinbasePrimeClient()
    data = client.get_staking_rewards(pid, symbol, start_date, end_date)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No staking rewards found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for r in data:
            amount = format_amount(r.get("amount"))
            rows.append([r.get("date", ""), r.get("symbol", ""), amount])
        print_markdown_table(["Date", "Symbol", "Amount"], rows)
        return

    table = Table(title=f"Staking Rewards ({len(data)})")
    table.add_column("Date", style="dim")
    table.add_column("Symbol", style="cyan")
    table.add_column("Amount", style="green", justify="right")

    for r in data:
        amount = format_amount(r.get("amount"))
        table.add_row(r.get("date", ""), r.get("symbol", ""), amount)

    console.print(table)


@app.command()
def assets(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List supported assets."""
    from .client import CoinbasePrimeClient

    client = CoinbasePrimeClient()
    data = client.list_assets()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No assets found.[/]")
        raise typer.Exit()

    if markdown:
        rows = [[a.get("symbol", ""), a.get("name", "")] for a in data[:50]]
        print_markdown_table(["Symbol", "Name"], rows)
        if len(data) > 50:
            print(f"\n... and {len(data) - 50} more assets")
        return

    table = Table(title=f"Assets ({len(data)})")
    table.add_column("Symbol", style="cyan")
    table.add_column("Name", style="white")

    for a in data[:50]:
        table.add_row(a.get("symbol", ""), a.get("name", ""))

    console.print(table)
    if len(data) > 50:
        console.print(f"[dim]... and {len(data) - 50} more assets[/]")


@app.command()
def activities(
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    symbols: str = typer.Option(None, "--symbols", "-s", help="Filter by symbols"),
    categories: str = typer.Option(None, "--categories", "-c", help="Filter by categories"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List activities."""
    from .client import CoinbasePrimeClient

    suffix = resolve_portfolio(fund)
    if suffix:
        set_credentials_for_suffix(suffix)
    pid = get_portfolio_id(fund)
    client = CoinbasePrimeClient()
    symbol_list = symbols.split(",") if symbols else None
    category_list = categories.split(",") if categories else None
    data = client.list_activities(pid, symbol_list, category_list, limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No activities found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for a in data:
            rows.append(
                [
                    a.get("created_at", "")[:10],
                    a.get("category", ""),
                    a.get("type", ""),
                    a.get("symbol", ""),
                    a.get("status", ""),
                ]
            )
        print_markdown_table(["Date", "Category", "Type", "Symbol", "Status"], rows)
        return

    table = Table(title=f"Activities ({len(data)})")
    table.add_column("Date", style="dim", max_width=12)
    table.add_column("Category", style="cyan")
    table.add_column("Type", style="white")
    table.add_column("Symbol", style="green")
    table.add_column("Status", style="yellow")

    for a in data:
        table.add_row(
            a.get("created_at", "")[:10],
            a.get("category", ""),
            a.get("type", ""),
            a.get("symbol", ""),
            a.get("status", ""),
        )

    console.print(table)


@app.command()
def raw(
    endpoint: str = typer.Argument(..., help="API endpoint (e.g., /portfolios or /assets)"),
    fund: str = typer.Option(None, "--fund", "-f", help="Portfolio name"),
    method: str = typer.Option("GET", "--method", "-X", help="HTTP method"),
    data: str = typer.Option(None, "--data", "-d", help="JSON request body"),
):
    """Make a raw API call.

    Examples:
        coinbase raw /portfolios
        coinbase raw /assets
        coinbase raw /portfolios/PORTFOLIO_ID/balances
    """
    from .client import CoinbasePrimeClient

    suffix = resolve_portfolio(fund)
    if suffix:
        set_credentials_for_suffix(suffix)

    body = json.loads(data) if data else None

    try:
        client = CoinbasePrimeClient()
        result = client.raw_request(endpoint, method=method, body=body)
        print(json.dumps(result, indent=2))
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
