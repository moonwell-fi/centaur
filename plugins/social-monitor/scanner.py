import json
import logging
import subprocess

from rich.console import Console

from .db import get_people_with_twitter, save_post

logger = logging.getLogger(__name__)
console = Console()


def fetch_timeline(twitter_handle: str, limit: int = 20) -> list[dict]:
    try:
        result = subprocess.run(
            ["ptwittercli", "timeline", twitter_handle, "--json", "-n", str(limit)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("ptwittercli failed for @%s: %s", twitter_handle, result.stderr.strip())
            return []
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("ptwittercli timed out for @%s", twitter_handle)
        return []
    except json.JSONDecodeError:
        logger.warning("Failed to parse ptwittercli output for @%s", twitter_handle)
        return []
    except FileNotFoundError:
        logger.warning("ptwittercli not found in PATH")
        return []


def scan_all(conn, limit_per_person: int = 20) -> int:
    people = get_people_with_twitter(conn)
    if not people:
        console.print("[yellow]No people with Twitter handles found.[/yellow]")
        return 0

    new_posts = 0
    console.print(f"[bold]Scanning {len(people)} Twitter accounts...[/bold]")

    for person in people:
        handle = person["twitter_handle"]
        console.print(f"  Scanning @{handle}...", end=" ")
        tweets = fetch_timeline(handle, limit=limit_per_person)

        person_new = 0
        for tweet in tweets:
            content = tweet.get("text", tweet.get("content", ""))
            post_url = tweet.get("url", tweet.get("link", ""))
            posted_at = tweet.get("created_at", tweet.get("date", ""))

            if not content:
                continue

            result = save_post(
                conn,
                person_id=person["id"],
                content=content,
                post_url=post_url or None,
                posted_at=posted_at or None,
                platform="twitter",
            )
            if result is not None:
                person_new += 1

        new_posts += person_new
        console.print(f"[green]{person_new} new[/green]")

    console.print(f"\n[bold green]Scan complete: {new_posts} new posts saved.[/bold green]")
    return new_posts
