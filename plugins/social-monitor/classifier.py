import json
import logging
import os

from anthropic import Anthropic
from rich.console import Console

from .db import get_unprocessed_posts, save_signal

logger = logging.getLogger(__name__)
console = Console()

CLASSIFICATION_PROMPT = """You are analyzing social media posts for career-related signals. Classify the post into one of these categories:

- JOB_CHANGE: Indicates the person is changing jobs, leaving a company, starting a new role, or "excited to announce" a new position
- STEALTH: Hints at working on an unannounced project, "building something new," vague teasers about upcoming work
- FOUNDING: Starting a new company, raising funding, looking for co-founders, incorporating
- NONE: No relevant career signal detected

Person: {person_name}
Current/Previous Company: {company}

Post:
{content}

Respond with ONLY a JSON object (no markdown, no code fences):
{{"signal_type": "...", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""


def classify_post(content: str, person_name: str, company: str | None = None) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return None

    client = Anthropic(api_key=api_key)
    prompt = CLASSIFICATION_PROMPT.format(
        person_name=person_name,
        company=company or "Unknown",
        content=content,
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        result = json.loads(response_text)

        if result.get("signal_type") == "NONE":
            return None

        return {
            "signal_type": result["signal_type"],
            "confidence": float(result["confidence"]),
            "reasoning": result.get("reasoning", ""),
        }
    except json.JSONDecodeError:
        logger.warning("Failed to parse classifier response for %s", person_name)
        return None
    except Exception:
        logger.exception("Classification error for %s", person_name)
        return None


def classify_unprocessed(conn) -> int:
    posts = get_unprocessed_posts(conn)
    if not posts:
        console.print("[yellow]No unprocessed posts found.[/yellow]")
        return 0

    console.print(f"[bold]Classifying {len(posts)} posts...[/bold]")
    signals_detected = 0

    for post in posts:
        console.print(f"  Classifying post from {post['person_name']}...", end=" ")
        result = classify_post(
            content=post["content"],
            person_name=post["person_name"],
            company=post.get("person_company"),
        )

        if result:
            save_signal(
                conn,
                post_id=post["id"],
                signal_type=result["signal_type"],
                confidence=result["confidence"],
                reasoning=result.get("reasoning"),
            )
            signals_detected += 1
            console.print(
                f"[bold red]{result['signal_type']}[/bold red] ({result['confidence']:.0%})"
            )
        else:
            save_signal(
                conn,
                post_id=post["id"],
                signal_type="NONE",
                confidence=0.0,
                reasoning="No career signal detected",
            )
            console.print("[dim]no signal[/dim]")

    console.print(
        f"\n[bold green]Classification complete: {signals_detected} signals detected.[/bold green]"
    )
    return signals_detected
