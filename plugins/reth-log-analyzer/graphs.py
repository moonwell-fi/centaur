"""Generate performance graphs from block metrics."""

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from .parser import BlockMetrics


def metrics_to_dataframe(blocks: list[BlockMetrics]) -> pd.DataFrame:
    """Convert block metrics to a pandas DataFrame."""
    data = []
    for b in blocks:
        state_root_ms = b.state_root_elapsed_ms or 0.0
        execution_ms = max(0.0, b.elapsed_ms - state_root_ms)
        state_root_pct = (state_root_ms / b.elapsed_ms * 100) if b.elapsed_ms > 0 else 0
        execution_pct = 100.0 - state_root_pct

        data.append(
            {
                "timestamp": b.timestamp,
                "block_number": b.block_number,
                "txs": b.txs,
                "gas_used_mgas": b.gas_used_mgas,
                "gas_throughput_ggas_s": b.gas_throughput_mgas_s / 1000.0,
                "gas_limit_mgas": b.gas_limit_mgas,
                "full_pct": b.full_pct,
                "base_fee_gwei": b.base_fee_gwei,
                "blobs": b.blobs,
                "elapsed_ms": b.elapsed_ms,
                "state_root_ms": state_root_ms,
                "execution_ms": execution_ms,
                "state_root_pct": state_root_pct,
                "execution_pct": execution_pct,
            }
        )
    return pd.DataFrame(data)


def plot_gas_throughput(
    df: pd.DataFrame, output_path: Path, min_gas_mgas: float = 0.0, title_suffix: str = ""
) -> Path:
    """Plot gas throughput over time."""
    filtered = df[df["gas_used_mgas"] > min_gas_mgas].copy()
    if filtered.empty:
        raise ValueError(f"No blocks with gas > {min_gas_mgas} Mgas")

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.plot(
        filtered["block_number"],
        filtered["gas_throughput_ggas_s"],
        "b-",
        linewidth=0.5,
        alpha=0.7,
    )
    ax.scatter(
        filtered["block_number"],
        filtered["gas_throughput_ggas_s"],
        c="blue",
        s=10,
        alpha=0.5,
    )

    avg_throughput = filtered["gas_throughput_ggas_s"].mean()
    ax.axhline(
        y=avg_throughput, color="red", linestyle="--", label=f"Avg: {avg_throughput:.2f} Ggas/s"
    )

    ax.set_xlabel("Block Number")
    ax.set_ylabel("Gas Throughput (Ggas/s)")
    title = "Gas Throughput Over Time"
    if title_suffix:
        title += f" ({title_suffix})"
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def plot_latency_breakdown(
    df: pd.DataFrame, output_path: Path, min_gas_mgas: float = 0.0, title_suffix: str = ""
) -> Path:
    """Plot latency breakdown (state root vs execution) as stacked bar chart."""
    filtered = df[df["gas_used_mgas"] > min_gas_mgas].copy()
    if filtered.empty:
        raise ValueError(f"No blocks with gas > {min_gas_mgas} Mgas")

    fig, ax = plt.subplots(figsize=(12, 6))

    width = 0.8
    x = range(len(filtered))

    ax.bar(
        x,
        filtered["execution_ms"].values,
        width,
        label="Execution",
        color="steelblue",
    )
    ax.bar(
        x,
        filtered["state_root_ms"].values,
        width,
        bottom=filtered["execution_ms"].values,
        label="State Root",
        color="coral",
    )

    step = max(1, len(filtered) // 10)
    ax.set_xticks(x[::step])
    ax.set_xticklabels(filtered["block_number"].values[::step], rotation=45, ha="right")

    ax.set_xlabel("Block Number")
    ax.set_ylabel("Latency (ms)")
    title = "Block Latency Breakdown"
    if title_suffix:
        title += f" ({title_suffix})"
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def plot_latency_percentage(
    df: pd.DataFrame, output_path: Path, min_gas_mgas: float = 0.0, title_suffix: str = ""
) -> Path:
    """Plot state root vs execution as percentage of total latency."""
    filtered = df[df["gas_used_mgas"] > min_gas_mgas].copy()
    if filtered.empty:
        raise ValueError(f"No blocks with gas > {min_gas_mgas} Mgas")

    fig, ax = plt.subplots(figsize=(12, 6))

    x = range(len(filtered))
    width = 0.8

    ax.bar(
        x,
        filtered["execution_pct"].values,
        width,
        label="Execution %",
        color="steelblue",
    )
    ax.bar(
        x,
        filtered["state_root_pct"].values,
        width,
        bottom=filtered["execution_pct"].values,
        label="State Root %",
        color="coral",
    )

    step = max(1, len(filtered) // 10)
    ax.set_xticks(x[::step])
    ax.set_xticklabels(filtered["block_number"].values[::step], rotation=45, ha="right")

    avg_execution = filtered["execution_pct"].mean()
    avg_state_root = filtered["state_root_pct"].mean()

    ax.set_xlabel("Block Number")
    ax.set_ylabel("Percentage of Total Latency")
    title = (
        f"Latency Breakdown % (Avg: Exec {avg_execution:.1f}% / State Root {avg_state_root:.1f}%)"
    )
    if title_suffix:
        title += f" ({title_suffix})"
    ax.set_title(title)
    ax.legend(loc="upper right")
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def plot_gas_vs_latency_scatter(
    df: pd.DataFrame, output_path: Path, min_gas_mgas: float = 0.0, title_suffix: str = ""
) -> Path:
    """Scatter plot of gas used vs latency with trend line."""
    filtered = df[df["gas_used_mgas"] > min_gas_mgas].copy()
    if filtered.empty:
        raise ValueError(f"No blocks with gas > {min_gas_mgas} Mgas")

    fig, ax = plt.subplots(figsize=(10, 8))

    ax.scatter(
        filtered["gas_used_mgas"],
        filtered["elapsed_ms"],
        c="steelblue",
        s=30,
        alpha=0.6,
        edgecolors="none",
    )

    if len(filtered) > 1:
        import numpy as np

        z = np.polyfit(filtered["gas_used_mgas"], filtered["elapsed_ms"], 1)
        p = np.poly1d(z)
        x_line = np.linspace(filtered["gas_used_mgas"].min(), filtered["gas_used_mgas"].max(), 100)
        ax.plot(x_line, p(x_line), "r--", linewidth=2, label=f"Trend: {z[0]:.2f} ms/Mgas")

    ax.set_xlabel("Gas Used (Mgas)")
    ax.set_ylabel("Total Latency (ms)")
    title = "Gas vs Latency"
    if title_suffix:
        title += f" ({title_suffix})"
    ax.set_title(title)
    if len(filtered) > 1:
        ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def generate_all_graphs(
    blocks: list[BlockMetrics],
    output_dir: Path,
    min_gas_mgas: float = 0.0,
    title_suffix: str = "",
) -> list[Path]:
    """Generate all performance graphs and return paths to created files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    df = metrics_to_dataframe(blocks)

    suffix = f"_min{int(min_gas_mgas)}mgas" if min_gas_mgas > 0 else ""
    paths = []

    paths.append(
        plot_gas_throughput(
            df,
            output_dir / f"gas_throughput{suffix}.png",
            min_gas_mgas=min_gas_mgas,
            title_suffix=title_suffix,
        )
    )
    paths.append(
        plot_latency_breakdown(
            df,
            output_dir / f"latency_breakdown{suffix}.png",
            min_gas_mgas=min_gas_mgas,
            title_suffix=title_suffix,
        )
    )
    paths.append(
        plot_latency_percentage(
            df,
            output_dir / f"latency_percentage{suffix}.png",
            min_gas_mgas=min_gas_mgas,
            title_suffix=title_suffix,
        )
    )
    paths.append(
        plot_gas_vs_latency_scatter(
            df,
            output_dir / f"gas_vs_latency{suffix}.png",
            min_gas_mgas=min_gas_mgas,
            title_suffix=title_suffix,
        )
    )

    return paths
