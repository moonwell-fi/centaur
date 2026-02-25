"""CLI for BitGo API."""

import json

import typer
from dotenv import load_dotenv
from rich.console import Console
from shared.cli_tables import Table

from .client import BitGoClient

load_dotenv()

app = typer.Typer(name="bitgo", help="BitGo CLI for wallet management and transaction monitoring")
console = Console()


def format_crypto(value: int | str | None, decimals: int = 8) -> str:
    """Format crypto amount from satoshis/wei to human-readable."""
    if value is None:
        return "0"
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return value
    if value == 0:
        return "0"
    amount = value / (10**decimals)
    if amount >= 1_000_000:
        return f"{amount:,.2f}"
    elif amount >= 1:
        return f"{amount:,.6f}"
    else:
        return f"{amount:.8f}"


def format_usd(value: float | None) -> str:
    """Format USD amount."""
    if value is None:
        return "$0.00"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    elif value >= 1e6:
        return f"${value / 1e6:.2f}M"
    elif value >= 1e3:
        return f"${value / 1e3:.2f}K"
    return f"${value:,.2f}"


def print_markdown_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a markdown-formatted table."""
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        print("| " + " | ".join(str(cell) for cell in row) + " |")


def get_decimals(coin: str) -> int:
    """Get decimal places for a coin."""
    coin_lower = coin.lower()
    if coin_lower in ("eth", "teth", "gteth"):
        return 18
    if coin_lower.startswith("eth") or coin_lower.endswith("eth"):
        return 18
    return 8


@app.command()
def wallets(
    coin: str = typer.Option(None, "--coin", "-c", help="Filter by coin (e.g., btc, eth)"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List all wallets."""
    client = BitGoClient()
    data = client.list_wallets(coin=coin, limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No wallets found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for w in data:
            decimals = get_decimals(w.get("coin", "btc"))
            balance = format_crypto(w.get("balanceString"), decimals)
            rows.append(
                [
                    w.get("label", "Unnamed"),
                    w.get("coin", "").upper(),
                    w.get("id", "")[:12] + "...",
                    balance,
                ]
            )
        print_markdown_table(["Label", "Coin", "ID", "Balance"], rows)
        return

    table = Table(title=f"Wallets ({len(data)})")
    table.add_column("Label", style="cyan", max_width=30)
    table.add_column("Coin", style="green", max_width=10)
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Balance", style="yellow", justify="right")

    for w in data:
        decimals = get_decimals(w.get("coin", "btc"))
        balance = format_crypto(w.get("balanceString"), decimals)
        table.add_row(
            w.get("label", "Unnamed"),
            w.get("coin", "").upper(),
            w.get("id", "")[:12] + "...",
            balance,
        )

    console.print(table)


@app.command()
def wallet(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    coin: str = typer.Option("btc", "--coin", "-c", help="Coin type (e.g., btc, eth)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown"),
):
    """Get wallet details."""
    client = BitGoClient()
    data = client.get_wallet(coin, wallet_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    decimals = get_decimals(data.get("coin", coin))
    balance = format_crypto(data.get("balanceString"), decimals)
    confirmed = format_crypto(data.get("confirmedBalanceString"), decimals)
    spendable = format_crypto(data.get("spendableBalanceString"), decimals)

    if markdown:
        print(f"## {data.get('label', 'Wallet')}\n")
        print(f"- **Coin**: {data.get('coin', '').upper()}")
        print(f"- **ID**: `{data.get('id', '')}`")
        print(f"- **Balance**: {balance}")
        print(f"- **Confirmed**: {confirmed}")
        print(f"- **Spendable**: {spendable}")
        return

    console.print(f"\n[bold cyan]{data.get('label', 'Wallet')}[/]")
    console.print(f"[dim]ID: {data.get('id', '')}[/]\n")
    console.print(f"Coin: [green]{data.get('coin', '').upper()}[/]")
    console.print(f"Balance: [yellow]{balance}[/]")
    console.print(f"Confirmed: [yellow]{confirmed}[/]")
    console.print(f"Spendable: [yellow]{spendable}[/]")

    if data.get("receiveAddress"):
        console.print(
            f"\nReceive Address: [cyan]{data.get('receiveAddress', {}).get('address', '')}[/]"
        )


@app.command()
def balances(
    enterprise: str = typer.Option(None, "--enterprise", "-e", help="Enterprise ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """Get total balances across all wallets."""
    client = BitGoClient()
    data = client.get_total_balances(enterprise_id=enterprise)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    balances_data = data.get("balances", {})
    if not balances_data:
        console.print("[yellow]No balances found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for coin, info in sorted(balances_data.items()):
            decimals = get_decimals(coin)
            balance = format_crypto(info.get("balanceString"), decimals)
            rows.append([coin.upper(), balance])
        print_markdown_table(["Coin", "Balance"], rows)
        return

    table = Table(title="Total Balances")
    table.add_column("Coin", style="cyan")
    table.add_column("Balance", style="yellow", justify="right")

    for coin, info in sorted(balances_data.items()):
        decimals = get_decimals(coin)
        balance = format_crypto(info.get("balanceString"), decimals)
        table.add_row(coin.upper(), balance)

    console.print(table)


@app.command()
def balance(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    coin: str = typer.Option("btc", "--coin", "-c", help="Coin type"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown"),
):
    """Get specific wallet balance."""
    client = BitGoClient()
    data = client.get_wallet_balance(coin, wallet_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    decimals = get_decimals(data.get("coin", coin))
    balance = format_crypto(data.get("balanceString"), decimals)
    confirmed = format_crypto(data.get("confirmedBalanceString"), decimals)
    spendable = format_crypto(data.get("spendableBalanceString"), decimals)

    if markdown:
        print(f"**{data.get('label', 'Wallet')}** ({data.get('coin', '').upper()})\n")
        print(f"- Balance: {balance}")
        print(f"- Confirmed: {confirmed}")
        print(f"- Spendable: {spendable}")
        return

    console.print(f"\n[bold]{data.get('label', 'Wallet')}[/] ({data.get('coin', '').upper()})")
    console.print(f"Balance: [yellow]{balance}[/]")
    console.print(f"Confirmed: [yellow]{confirmed}[/]")
    console.print(f"Spendable: [yellow]{spendable}[/]")


@app.command()
def transactions(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    coin: str = typer.Option("btc", "--coin", "-c", help="Coin type"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List transactions for a wallet."""
    client = BitGoClient()
    data = client.list_transactions(coin, wallet_id, limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No transactions found.[/]")
        raise typer.Exit()

    decimals = get_decimals(coin)

    if markdown:
        rows = []
        for tx in data:
            value = format_crypto(tx.get("valueString"), decimals)
            tx_type = tx.get("type", "")
            date = tx.get("date", "")[:10]
            state = tx.get("state", "")
            txid = tx.get("txid", "")[:16] + "..." if tx.get("txid") else ""
            rows.append([date, tx_type, value, state, txid])
        print_markdown_table(["Date", "Type", "Value", "State", "TxID"], rows)
        return

    table = Table(title=f"Transactions ({len(data)})")
    table.add_column("Date", style="dim", max_width=12)
    table.add_column("Type", style="cyan", max_width=10)
    table.add_column("Value", style="yellow", justify="right")
    table.add_column("State", style="green", max_width=12)
    table.add_column("TxID", style="dim", max_width=20)

    for tx in data:
        value = format_crypto(tx.get("valueString"), decimals)
        tx_type = tx.get("type", "")
        date = tx.get("date", "")[:10]
        state = tx.get("state", "")
        txid = tx.get("txid", "")[:16] + "..." if tx.get("txid") else ""
        table.add_row(date, tx_type, value, state, txid)

    console.print(table)


@app.command()
def transaction(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    txid: str = typer.Argument(..., help="Transaction/Transfer ID"),
    coin: str = typer.Option("btc", "--coin", "-c", help="Coin type"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown"),
):
    """Get transaction details."""
    client = BitGoClient()
    data = client.get_transaction(coin, wallet_id, txid)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    decimals = get_decimals(coin)
    value = format_crypto(data.get("valueString"), decimals)
    fee = format_crypto(data.get("feeString"), decimals)

    if markdown:
        print("## Transaction Details\n")
        print(f"- **TxID**: `{data.get('txid', '')}`")
        print(f"- **Type**: {data.get('type', '')}")
        print(f"- **Value**: {value}")
        print(f"- **Fee**: {fee}")
        print(f"- **State**: {data.get('state', '')}")
        print(f"- **Date**: {data.get('date', '')}")
        print(f"- **Confirmations**: {data.get('confirmations', 0)}")
        return

    console.print("\n[bold]Transaction[/]")
    console.print(f"[dim]TxID: {data.get('txid', '')}[/]\n")
    console.print(f"Type: [cyan]{data.get('type', '')}[/]")
    console.print(f"Value: [yellow]{value}[/]")
    console.print(f"Fee: [yellow]{fee}[/]")
    console.print(f"State: [green]{data.get('state', '')}[/]")
    console.print(f"Date: {data.get('date', '')}")
    console.print(f"Confirmations: {data.get('confirmations', 0)}")

    entries = data.get("entries", [])
    if entries:
        console.print(f"\n[bold]Entries ({len(entries)}):[/]")
        for entry in entries[:5]:
            addr = (
                entry.get("address", "")[:20] + "..."
                if len(entry.get("address", "")) > 20
                else entry.get("address", "")
            )
            entry_value = format_crypto(entry.get("valueString"), decimals)
            console.print(f"  {addr}: [yellow]{entry_value}[/]")


@app.command()
def addresses(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    coin: str = typer.Option("btc", "--coin", "-c", help="Coin type"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List addresses for a wallet."""
    client = BitGoClient()
    data = client.list_addresses(coin, wallet_id, limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No addresses found.[/]")
        raise typer.Exit()

    decimals = get_decimals(coin)

    if markdown:
        rows = []
        for addr in data:
            address = addr.get("address", "")
            balance = format_crypto(addr.get("balance", {}).get("string"), decimals)
            label = addr.get("label", "")
            rows.append([address[:30] + "...", label, balance])
        print_markdown_table(["Address", "Label", "Balance"], rows)
        return

    table = Table(title=f"Addresses ({len(data)})")
    table.add_column("Address", style="cyan", max_width=45)
    table.add_column("Label", style="white", max_width=20)
    table.add_column("Balance", style="yellow", justify="right")

    for addr in data:
        address = addr.get("address", "")
        balance_info = addr.get("balance", {})
        balance = format_crypto(
            balance_info.get("string") if isinstance(balance_info, dict) else balance_info, decimals
        )
        label = addr.get("label", "")
        table.add_row(address, label, balance)

    console.print(table)


@app.command()
def enterprises(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List enterprises."""
    client = BitGoClient()
    data = client.list_enterprises()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No enterprises found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for ent in data:
            rows.append(
                [
                    ent.get("name", ""),
                    ent.get("id", "")[:12] + "...",
                    ent.get("primaryContact", ""),
                ]
            )
        print_markdown_table(["Name", "ID", "Primary Contact"], rows)
        return

    table = Table(title=f"Enterprises ({len(data)})")
    table.add_column("Name", style="cyan", max_width=30)
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Primary Contact", style="white", max_width=30)

    for ent in data:
        table.add_row(
            ent.get("name", ""),
            ent.get("id", "")[:12] + "...",
            ent.get("primaryContact", ""),
        )

    console.print(table)


@app.command()
def raw(
    endpoint: str = typer.Argument(..., help="API endpoint (e.g., /btc/wallet or /enterprise)"),
    method: str = typer.Option("GET", "--method", "-X", help="HTTP method"),
    data: str = typer.Option(None, "--data", "-d", help="JSON request body"),
):
    """Make a raw API call.

    Examples:
        bitgo raw /btc/wallet
        bitgo raw /enterprise
        bitgo raw /eth/wallet/WALLET_ID/transfer --method GET
    """
    client = BitGoClient()
    kwargs = {}
    if data:
        kwargs["json"] = json.loads(data)

    try:
        result = client.raw_request(endpoint, method=method, **kwargs)
        print(json.dumps(result, indent=2))
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)


# Staking commands
@app.command("staking-coins")
def staking_coins(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List coins available for staking."""
    client = BitGoClient()
    data = client.list_staking_coins()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No staking coins found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for c in data:
            apy = c.get("apy", c.get("expectedApy", ""))
            apy_str = f"{float(apy):.2f}%" if apy else ""
            rows.append([c.get("coin", "").upper(), c.get("name", ""), apy_str])
        print_markdown_table(["Coin", "Name", "APY"], rows)
        return

    table = Table(title="Staking Coins")
    table.add_column("Coin", style="cyan", max_width=12)
    table.add_column("Name", style="white", max_width=30)
    table.add_column("APY", style="green", justify="right", max_width=10)

    for c in data:
        apy = c.get("apy", c.get("expectedApy", ""))
        apy_str = f"{float(apy):.2f}%" if apy else ""
        table.add_row(c.get("coin", "").upper(), c.get("name", ""), apy_str)

    console.print(table)


@app.command("staking-requests")
def staking_requests(
    enterprise: str = typer.Option(None, "--enterprise", "-e", help="Enterprise ID"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List staking requests."""
    client = BitGoClient()
    data = client.list_staking_requests(enterprise_id=enterprise, limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No staking requests found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for r in data:
            rows.append(
                [
                    r.get("id", "")[:12] + "...",
                    r.get("coin", "").upper(),
                    r.get("type", ""),
                    r.get("status", ""),
                    r.get("createdAt", "")[:10],
                ]
            )
        print_markdown_table(["ID", "Coin", "Type", "Status", "Created"], rows)
        return

    table = Table(title="Staking Requests")
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Coin", style="cyan", max_width=10)
    table.add_column("Type", style="white", max_width=12)
    table.add_column("Status", style="green", max_width=15)
    table.add_column("Created", style="dim", max_width=12)

    for r in data:
        table.add_row(
            r.get("id", "")[:12] + "...",
            r.get("coin", "").upper(),
            r.get("type", ""),
            r.get("status", ""),
            r.get("createdAt", "")[:10],
        )

    console.print(table)


@app.command("staking-request")
def staking_request(
    request_id: str = typer.Argument(..., help="Staking request ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get details of a staking request."""
    client = BitGoClient()
    data = client.get_staking_request(request_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print("\n[bold]Staking Request[/]")
    console.print(f"[dim]ID: {data.get('id', '')}[/]\n")
    console.print(f"Coin: [cyan]{data.get('coin', '').upper()}[/]")
    console.print(f"Type: {data.get('type', '')}")
    console.print(f"Status: [green]{data.get('status', '')}[/]")
    console.print(f"Amount: {data.get('amount', '')}")
    console.print(f"Created: {data.get('createdAt', '')}")


@app.command("delegations")
def staking_delegations(
    wallet_id: str = typer.Argument(..., help="Wallet ID"),
    coin: str = typer.Option("eth", "--coin", "-c", help="Coin type"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List staking delegations for a wallet."""
    client = BitGoClient()
    data = client.list_staking_delegations(coin, wallet_id, limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No delegations found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for d in data:
            rows.append(
                [
                    d.get("id", "")[:12] + "...",
                    d.get("validator", "")[:20],
                    d.get("amount", ""),
                    d.get("status", ""),
                ]
            )
        print_markdown_table(["ID", "Validator", "Amount", "Status"], rows)
        return

    table = Table(title=f"Staking Delegations ({coin.upper()})")
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Validator", style="cyan", max_width=25)
    table.add_column("Amount", style="yellow", justify="right", max_width=18)
    table.add_column("Status", style="green", max_width=15)

    for d in data:
        table.add_row(
            d.get("id", "")[:12] + "...",
            d.get("validator", "")[:23],
            str(d.get("amount", "")),
            d.get("status", ""),
        )

    console.print(table)


@app.command("staking-rewards")
def staking_rewards(
    enterprise: str = typer.Argument(..., help="Enterprise ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List staking rewards for an enterprise."""
    client = BitGoClient()
    data = client.list_staking_rewards(enterprise)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No rewards found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for r in data:
            rows.append(
                [
                    r.get("coin", "").upper(),
                    r.get("amount", ""),
                    r.get("date", "")[:10],
                ]
            )
        print_markdown_table(["Coin", "Amount", "Date"], rows)
        return

    table = Table(title="Staking Rewards")
    table.add_column("Coin", style="cyan", max_width=12)
    table.add_column("Amount", style="yellow", justify="right", max_width=18)
    table.add_column("Date", style="dim", max_width=12)

    for r in data:
        table.add_row(
            r.get("coin", "").upper(),
            str(r.get("amount", "")),
            r.get("date", "")[:10],
        )

    console.print(table)


@app.command("validators")
def staking_validators(
    coin: str = typer.Option(None, "--coin", "-c", help="Filter by coin"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    markdown: bool = typer.Option(False, "--markdown", "-m", help="Output as markdown table"),
):
    """List partnered validators for staking."""
    client = BitGoClient()
    data = client.list_partnered_validators(coin=coin)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No validators found.[/]")
        raise typer.Exit()

    if markdown:
        rows = []
        for v in data:
            rows.append(
                [
                    v.get("name", ""),
                    v.get("coin", "").upper(),
                    v.get("address", "")[:25] + "...",
                ]
            )
        print_markdown_table(["Name", "Coin", "Address"], rows)
        return

    table = Table(title="Partnered Validators")
    table.add_column("Name", style="cyan", max_width=30)
    table.add_column("Coin", style="white", max_width=12)
    table.add_column("Address", style="dim", max_width=30)

    for v in data:
        address = v.get("address", "")
        address_short = address[:27] + "..." if len(address) > 30 else address
        table.add_row(v.get("name", ""), v.get("coin", "").upper(), address_short)

    console.print(table)


if __name__ == "__main__":
    app()
