"""CLI for Anchorage Digital custody."""

import json

import typer
from dotenv import load_dotenv
from rich.console import Console
from shared.cli_tables import Table

from .client import AnchorageClient

load_dotenv()

app = typer.Typer(name="anchorage", help="Anchorage Digital custody CLI")
console = Console()

FundOption = typer.Option("pf", "--fund", "-f", help="Fund: pf (Paradigm Fund), p1, p2")
JsonOption = typer.Option(False, "--json", "-j", help="Output as JSON")


def get_client(fund: str = "pf"):
    return AnchorageClient(fund=fund)


def format_amount(value: float | str | None, decimals: int = 4) -> str:
    """Format amount with appropriate precision."""
    if value is None:
        return "0"
    try:
        val = float(value)
        if val >= 1e9:
            return f"{val / 1e9:.2f}B"
        elif val >= 1e6:
            return f"{val / 1e6:.2f}M"
        elif val >= 1e3:
            return f"{val / 1e3:.2f}K"
        return f"{val:.{decimals}f}"
    except (ValueError, TypeError):
        return str(value)


@app.command()
def vaults(
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """List all vaults."""
    client = get_client(fund)
    data = client.list_vaults()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No vaults found.[/]")
        raise typer.Exit()

    table = Table(title=f"Vaults [{client.fund_name}]")
    table.add_column("ID", style="cyan", max_width=20)
    table.add_column("Name", style="white", max_width=30)
    table.add_column("Type", style="green", max_width=15)
    table.add_column("Status", style="yellow", max_width=12)

    for vault in data:
        vault_id = vault.get("vault_id", vault.get("id", ""))
        name = vault.get("name", "")
        vault_type = vault.get("type", vault.get("vault_type", ""))
        status = vault.get("status", "")
        table.add_row(
            vault_id[:18] + ".." if len(vault_id) > 20 else vault_id, name, vault_type, status
        )

    console.print(table)


@app.command()
def vault(
    vault_id: str = typer.Argument(..., help="Vault ID"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """Get vault details."""
    client = get_client(fund)
    data = client.get_vault(vault_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold cyan]Vault[/] [{client.fund_name}]\n")
    console.print(f"[bold]ID:[/] {data.get('vault_id', data.get('id', ''))}")
    console.print(f"[bold]Name:[/] {data.get('name', '')}")
    console.print(f"[bold]Type:[/] {data.get('type', data.get('vault_type', ''))}")
    console.print(f"[bold]Status:[/] {data.get('status', '')}")

    if data.get("created_at"):
        console.print(f"[bold]Created:[/] {data.get('created_at', '')}")


@app.command()
def balances(
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """Get all balances across vaults."""
    client = get_client(fund)
    data = client.get_balances()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No balances found.[/]")
        raise typer.Exit()

    table = Table(title=f"Balances [{client.fund_name}]")
    table.add_column("Asset", style="cyan", max_width=15)
    table.add_column("Balance", style="yellow", justify="right", max_width=20)
    table.add_column("USD Value", style="green", justify="right", max_width=15)
    table.add_column("Vault", style="dim", max_width=20)

    for bal in data:
        asset = bal.get("asset", bal.get("symbol", bal.get("currency", "")))
        amount = format_amount(bal.get("balance", bal.get("amount", 0)))
        usd_value = bal.get("usd_value", bal.get("value_usd", ""))
        usd_str = f"${format_amount(usd_value)}" if usd_value else ""
        vault_name = bal.get("vault_name", bal.get("vault_id", ""))[:18]
        table.add_row(asset, amount, usd_str, vault_name)

    console.print(table)


@app.command()
def balance(
    vault_id: str = typer.Argument(..., help="Vault ID"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """Get balance for a specific vault."""
    client = get_client(fund)
    data = client.get_vault_balance(vault_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No balances in vault.[/]")
        raise typer.Exit()

    table = Table(title=f"Vault Balance [{client.fund_name}]")
    table.add_column("Asset", style="cyan", max_width=15)
    table.add_column("Balance", style="yellow", justify="right", max_width=20)
    table.add_column("USD Value", style="green", justify="right", max_width=15)

    for bal in data:
        asset = bal.get("asset", bal.get("symbol", bal.get("currency", "")))
        amount = format_amount(bal.get("balance", bal.get("amount", 0)))
        usd_value = bal.get("usd_value", bal.get("value_usd", ""))
        usd_str = f"${format_amount(usd_value)}" if usd_value else ""
        table.add_row(asset, amount, usd_str)

    console.print(table)


@app.command()
def transactions(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    vault_id: str = typer.Option(None, "--vault", "-v", help="Filter by vault ID"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """List transactions."""
    client = get_client(fund)
    data = client.list_transactions(limit=limit, vault_id=vault_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No transactions found.[/]")
        raise typer.Exit()

    table = Table(title=f"Transactions [{client.fund_name}]")
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Type", style="cyan", max_width=12)
    table.add_column("Asset", style="white", max_width=10)
    table.add_column("Amount", style="yellow", justify="right", max_width=15)
    table.add_column("Status", style="green", max_width=12)
    table.add_column("Date", style="dim", max_width=12)

    for tx in data:
        tx_id = tx.get("transaction_id", tx.get("id", ""))[:10]
        tx_type = tx.get("type", tx.get("transaction_type", ""))
        asset = tx.get("asset", tx.get("symbol", tx.get("currency", "")))
        amount = format_amount(tx.get("amount", 0))
        status = tx.get("status", "")
        date = tx.get("created_at", tx.get("timestamp", ""))[:10]
        table.add_row(tx_id + "..", tx_type, asset, amount, status, date)

    console.print(table)


@app.command()
def addresses(
    vault_id: str = typer.Argument(..., help="Vault ID"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """Get deposit addresses for a vault."""
    client = get_client(fund)
    data = client.get_addresses(vault_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No addresses found.[/]")
        raise typer.Exit()

    table = Table(title=f"Deposit Addresses [{client.fund_name}]")
    table.add_column("Asset", style="cyan", max_width=12)
    table.add_column("Network", style="green", max_width=15)
    table.add_column("Address", style="white", max_width=50)

    for addr in data:
        asset = addr.get("asset", addr.get("symbol", addr.get("currency", "")))
        network = addr.get("network", addr.get("chain", ""))
        address = addr.get("address", "")
        table.add_row(asset, network, address)

    console.print(table)


@app.command()
def raw(
    endpoint: str = typer.Argument(..., help="API endpoint (e.g., /vaults)"),
    method: str = typer.Option("GET", "--method", "-X", help="HTTP method"),
    params: str = typer.Option(None, "--params", "-p", help="Query params as key=value,key=value"),
    fund: str = FundOption,
):
    """Make a raw API call."""
    client = get_client(fund)

    query_params = None
    if params:
        query_params = {}
        for pair in params.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                query_params[k.strip()] = v.strip()

    try:
        data = client.raw_request(method.upper(), endpoint, params=query_params)
        print(json.dumps(data, indent=2))
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)


# Staking commands
@app.command()
def staking(
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """Get staking summary across all assets."""
    client = get_client(fund)
    data = client.get_staking_summary()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No staking data found.[/]")
        raise typer.Exit()

    console.print(f"\n[bold cyan]Staking Summary[/] [{client.fund_name}]\n")
    for key, value in data.items():
        if isinstance(value, dict):
            console.print(f"[bold]{key}:[/]")
            for k, v in value.items():
                console.print(f"  {k}: {format_amount(v) if isinstance(v, (int, float)) else v}")
        else:
            console.print(
                f"[bold]{key}:[/] {format_amount(value) if isinstance(value, (int, float)) else value}"
            )


@app.command()
def delegations(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """List staking delegations."""
    client = get_client(fund)
    data = client.list_staking_delegations(limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No delegations found.[/]")
        raise typer.Exit()

    table = Table(title=f"Staking Delegations [{client.fund_name}]")
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Asset", style="cyan", max_width=12)
    table.add_column("Amount", style="yellow", justify="right", max_width=18)
    table.add_column("Validator", style="white", max_width=25)
    table.add_column("Status", style="green", max_width=12)

    for d in data:
        delegation_id = d.get("delegationId", d.get("id", ""))[:12]
        asset = d.get("assetType", d.get("asset", ""))
        amount = format_amount(d.get("amount", d.get("stakedAmount", 0)))
        validator = d.get("validatorName", d.get("validator", ""))[:23]
        status = d.get("status", "")
        table.add_row(delegation_id + "..", asset, amount, validator, status)

    console.print(table)


@app.command()
def delegation(
    delegation_id: str = typer.Argument(..., help="Delegation ID"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """Get details of a specific staking delegation."""
    client = get_client(fund)
    data = client.get_staking_delegation(delegation_id)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold cyan]Delegation Details[/] [{client.fund_name}]\n")
    console.print(f"[bold]ID:[/] {data.get('delegationId', data.get('id', ''))}")
    console.print(f"[bold]Asset:[/] {data.get('assetType', data.get('asset', ''))}")
    console.print(
        f"[bold]Amount:[/] {format_amount(data.get('amount', data.get('stakedAmount', 0)))}"
    )
    console.print(f"[bold]Validator:[/] {data.get('validatorName', data.get('validator', ''))}")
    console.print(f"[bold]Status:[/] {data.get('status', '')}")

    if data.get("rewards"):
        console.print(f"[bold]Rewards:[/] {format_amount(data.get('rewards', 0))}")
    if data.get("createdAt"):
        console.print(f"[bold]Created:[/] {data.get('createdAt', '')}")


@app.command()
def rewards(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """List staking rewards."""
    client = get_client(fund)
    data = client.list_staking_rewards(limit=limit)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No rewards found.[/]")
        raise typer.Exit()

    table = Table(title=f"Staking Rewards [{client.fund_name}]")
    table.add_column("Date", style="dim", max_width=12)
    table.add_column("Asset", style="cyan", max_width=12)
    table.add_column("Amount", style="yellow", justify="right", max_width=18)
    table.add_column("Validator", style="white", max_width=25)
    table.add_column("Type", style="green", max_width=15)

    for r in data:
        date = r.get("date", r.get("createdAt", r.get("timestamp", "")))[:10]
        asset = r.get("assetType", r.get("asset", ""))
        amount = format_amount(r.get("amount", r.get("rewardAmount", 0)))
        validator = r.get("validatorName", r.get("validator", ""))[:23]
        reward_type = r.get("type", r.get("rewardType", ""))
        table.add_row(date, asset, amount, validator, reward_type)

    console.print(table)


@app.command()
def validators(
    asset: str = typer.Option(None, "--asset", "-a", help="Filter by asset type (e.g., ETH, SOL)"),
    fund: str = FundOption,
    json_output: bool = JsonOption,
):
    """List available validators for staking."""
    client = get_client(fund)
    data = client.list_validators(asset=asset)

    if json_output:
        print(json.dumps(data, indent=2))
        return

    if not data:
        console.print("[yellow]No validators found.[/]")
        raise typer.Exit()

    table = Table(title=f"Validators [{client.fund_name}]")
    table.add_column("Name", style="cyan", max_width=30)
    table.add_column("Asset", style="white", max_width=12)
    table.add_column("Address", style="dim", max_width=25)
    table.add_column("APY", style="green", justify="right", max_width=10)

    for v in data:
        name = v.get("name", v.get("validatorName", ""))[:28]
        asset_type = v.get("assetType", v.get("asset", ""))
        address = v.get("address", v.get("validatorAddress", ""))
        address_short = address[:22] + "..." if len(address) > 25 else address
        apy = v.get("apy", v.get("expectedApy", ""))
        apy_str = f"{float(apy):.2f}%" if apy else ""
        table.add_row(name, asset_type, address_short, apy_str)

    console.print(table)


if __name__ == "__main__":
    app()
