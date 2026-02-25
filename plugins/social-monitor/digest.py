import logging
import subprocess
from datetime import datetime

from rich.console import Console

from .db import get_unnotified_signals, mark_signals_notified

logger = logging.getLogger(__name__)
console = Console()

SIGNAL_EMOJI = {
    "FOUNDING": "🚀 Founding Signals",
    "JOB_CHANGE": "💼 Job Changes",
    "STEALTH": "🔮 Stealth Signals",
}


def format_signal(signal: dict) -> str:
    handle = signal.get("twitter_handle", "")
    handle_str = f" (@{handle})" if handle else ""
    company = signal.get("company", "")
    company_str = f" — {company}" if company else ""

    content = signal.get("post_content", "")
    if len(content) > 280:
        content = content[:277] + "..."

    lines = [
        f"*{signal['person_name']}*{handle_str}{company_str}",
        f"> {content}",
        f"Signal: {signal['signal_type']} ({signal['confidence']:.0%} confidence)",
    ]

    if signal.get("reasoning"):
        lines.append(signal["reasoning"])

    if signal.get("post_url"):
        lines.append(f"<{signal['post_url']}|View on Twitter>")

    return "\n".join(lines)


def send_digest(conn, channel: str | None = None) -> int:
    signals = get_unnotified_signals(conn, min_confidence=0.5)
    if not signals:
        console.print("[yellow]No new signals to send.[/yellow]")
        return 0

    target_channel = channel or "#ai-agent"
    today = datetime.now().strftime("%B %d, %Y")

    header = (
        f"*Social Feed Monitor — Daily Digest*\n"
        f"_{today}_\n\n"
        f"{len(signals)} career signal(s) detected:"
    )

    grouped: dict[str, list[dict]] = {}
    for signal in signals:
        st = signal["signal_type"]
        grouped.setdefault(st, []).append(signal)

    sections = [header]
    for signal_type in ["FOUNDING", "JOB_CHANGE", "STEALTH"]:
        if signal_type not in grouped:
            continue
        section_header = SIGNAL_EMOJI.get(signal_type, signal_type)
        sections.append(f"\n*{section_header}*")
        for signal in grouped[signal_type]:
            sections.append(format_signal(signal))

    message = "\n\n".join(sections)

    try:
        subprocess.run(
            ["slack", "send", target_channel, message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        console.print(
            f"[bold green]Digest sent to {target_channel}: {len(signals)} signals.[/bold green]"
        )
    except Exception:
        logger.exception("Failed to send Slack digest")
        console.print("[bold red]Failed to send Slack digest.[/bold red]")
        return 0

    signal_ids = [s["id"] for s in signals]
    mark_signals_notified(conn, signal_ids)
    return len(signals)
