"""CLI for reth log analyzer."""

from dotenv import load_dotenv

load_dotenv()

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from ai_v2.cli_tables import Table

from .graphs import generate_all_graphs, metrics_to_dataframe
from .parser import parse_log_file

app = typer.Typer(help="Parse reth logs and generate performance graphs")
console = Console()


@app.command()
def parse(
    log_file: Path = typer.Argument(..., help="Path to reth log file"),
    output: Optional[Path] = typer.Option(None, "-o", "--output", help="Output CSV file"),
    limit: int = typer.Option(0, "-n", "--limit", help="Limit number of blocks to show (0=all)"),
    min_gas: float = typer.Option(0.0, "--min-gas", help="Minimum gas in Mgas to include"),
):
    """Parse reth log file and display block metrics."""
    if not log_file.exists():
        console.print(f"[red]Error: File not found: {log_file}[/red]")
        raise typer.Exit(1)

    blocks = parse_log_file(log_file)
    if not blocks:
        console.print("[yellow]No blocks found in log file[/yellow]")
        raise typer.Exit(0)

    df = metrics_to_dataframe(blocks)
    if min_gas > 0:
        df = df[df["gas_used_mgas"] > min_gas]

    console.print(f"[green]Parsed {len(blocks)} blocks, {len(df)} after filtering[/green]")

    if output:
        df.to_csv(output, index=False)
        console.print(f"[green]Saved to {output}[/green]")
    else:
        display_df = df if limit == 0 else df.tail(limit)

        table = Table(title="Block Metrics")
        table.add_column("Block", justify="right")
        table.add_column("Txs", justify="right")
        table.add_column("Gas (Mgas)", justify="right")
        table.add_column("Throughput (Ggas/s)", justify="right")
        table.add_column("Latency (ms)", justify="right")
        table.add_column("State Root (ms)", justify="right")
        table.add_column("Exec %", justify="right")

        for _, row in display_df.iterrows():
            table.add_row(
                str(row["block_number"]),
                str(row["txs"]),
                f"{row['gas_used_mgas']:.1f}",
                f"{row['gas_throughput_ggas_s']:.2f}",
                f"{row['elapsed_ms']:.1f}",
                f"{row['state_root_ms']:.2f}",
                f"{row['execution_pct']:.1f}%",
            )

        console.print(table)

        if len(df) > 0:
            console.print("\n[bold]Summary:[/bold]")
            console.print(f"  Blocks: {len(df)}")
            console.print(f"  Avg throughput: {df['gas_throughput_ggas_s'].mean():.2f} Ggas/s")
            console.print(f"  Avg latency: {df['elapsed_ms'].mean():.1f} ms")
            console.print(f"  Avg execution %: {df['execution_pct'].mean():.1f}%")
            console.print(f"  Max latency: {df['elapsed_ms'].max():.1f} ms")
            console.print(f"  Max gas: {df['gas_used_mgas'].max():.1f} Mgas")


@app.command()
def graphs(
    log_file: Path = typer.Argument(..., help="Path to reth log file"),
    output_dir: Path = typer.Option(
        Path("."), "-o", "--output", help="Output directory for graphs"
    ),
    min_gas: float = typer.Option(0.0, "--min-gas", help="Minimum gas in Mgas to include"),
    title: str = typer.Option("", "--title", help="Title suffix for graphs"),
):
    """Generate performance graphs from reth log file."""
    if not log_file.exists():
        console.print(f"[red]Error: File not found: {log_file}[/red]")
        raise typer.Exit(1)

    blocks = parse_log_file(log_file)
    if not blocks:
        console.print("[yellow]No blocks found in log file[/yellow]")
        raise typer.Exit(0)

    console.print(f"[green]Parsed {len(blocks)} blocks[/green]")

    try:
        paths = generate_all_graphs(
            blocks,
            output_dir,
            min_gas_mgas=min_gas,
            title_suffix=title,
        )
        console.print(f"[green]Generated {len(paths)} graphs:[/green]")
        for p in paths:
            console.print(f"  - {p}")
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def summary(
    log_file: Path = typer.Argument(..., help="Path to reth log file"),
    min_gas: float = typer.Option(10.0, "--min-gas", help="Minimum gas in Mgas for 'big blocks'"),
    markdown: bool = typer.Option(False, "-m", "--markdown", help="Output as markdown"),
):
    """Generate performance summary report."""
    if not log_file.exists():
        console.print(f"[red]Error: File not found: {log_file}[/red]")
        raise typer.Exit(1)

    blocks = parse_log_file(log_file)
    if not blocks:
        console.print("[yellow]No blocks found in log file[/yellow]")
        raise typer.Exit(0)

    df = metrics_to_dataframe(blocks)
    total = len(df)
    empty = len(df[df["gas_used_mgas"] == 0])
    big_blocks = df[df["gas_used_mgas"] > min_gas]

    if markdown:
        print("## Reth Big Blocks Performance Analysis\n")
        print("### Dataset Overview")
        print(
            f"- **{total:,} blocks** analyzed ({df['block_number'].min()} - {df['block_number'].max()})"
        )
        print(f"- {empty:,} empty blocks, {total - empty:,} non-empty")
        print(
            f"- Max gas: **{df['gas_used_mgas'].max():.0f} Mgas** | Max latency: **{df['elapsed_ms'].max():.0f}ms**\n"
        )

        print("### Block Categories")
        print("| Category | Blocks | Avg Latency | State Root % | Execution % |")
        print("|----------|--------|-------------|--------------|-------------|")

        for name, subset in [
            ("Empty", df[df["gas_used_mgas"] == 0]),
            ("Light (<10M)", df[(df["gas_used_mgas"] > 0) & (df["gas_used_mgas"] <= 10)]),
            ("Medium (10-50M)", df[(df["gas_used_mgas"] > 10) & (df["gas_used_mgas"] <= 50)]),
            ("Big (50-500M)", df[(df["gas_used_mgas"] > 50) & (df["gas_used_mgas"] <= 500)]),
            ("Huge (>500M)", df[df["gas_used_mgas"] > 500]),
        ]:
            if len(subset) > 0:
                avg_lat = subset["elapsed_ms"].mean()
                avg_sr = subset["state_root_pct"].mean()
                avg_ex = subset["execution_pct"].mean()
                print(
                    f"| {name} | {len(subset):,} | {avg_lat:.1f}ms | {avg_sr:.1f}% | **{avg_ex:.1f}%** |"
                )

        if len(big_blocks) > 0:
            print(f"\n### Key Findings (blocks >{min_gas:.0f}M gas)")
            print(f"- **{len(big_blocks)} blocks** analyzed")
            print(f"- **Avg throughput:** {big_blocks['gas_throughput_ggas_s'].mean():.2f} Ggas/s")
            print(f"- **Execution:** {big_blocks['execution_pct'].mean():.1f}% of total time")
            print(f"- **State root:** {big_blocks['state_root_pct'].mean():.1f}% of total time")

            slowest = big_blocks.loc[big_blocks["elapsed_ms"].idxmax()]
            print(
                f"- **Slowest block:** #{int(slowest['block_number'])} at {slowest['elapsed_ms']:.0f}ms ({slowest['gas_used_mgas']:.0f}M gas)"
            )
    else:
        console.print("[bold]Reth Big Blocks Performance Analysis[/bold]\n")
        console.print(
            f"Blocks analyzed: {total:,} ({df['block_number'].min()} - {df['block_number'].max()})"
        )
        console.print(f"Empty: {empty:,} | Non-empty: {total - empty:,}")
        console.print(
            f"Max gas: {df['gas_used_mgas'].max():.0f} Mgas | Max latency: {df['elapsed_ms'].max():.0f}ms\n"
        )

        if len(big_blocks) > 0:
            console.print(f"[bold]Big blocks (>{min_gas:.0f}M gas): {len(big_blocks)}[/bold]")
            console.print(
                f"  Avg throughput: {big_blocks['gas_throughput_ggas_s'].mean():.2f} Ggas/s"
            )
            console.print(f"  Avg execution %: {big_blocks['execution_pct'].mean():.1f}%")
            console.print(f"  Avg state root %: {big_blocks['state_root_pct'].mean():.1f}%")


if __name__ == "__main__":
    app()
