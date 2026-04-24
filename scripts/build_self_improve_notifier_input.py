#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

PR_METADATA_START = "<!-- self_improve_metadata_v1:start -->"
PR_METADATA_END = "<!-- self_improve_metadata_v1:end -->"
_GITHUB_REPO_RE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/.]+?)(?:\.git)?$")


def _normalize_github_repo(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = _GITHUB_REPO_RE.search(raw)
    if match:
        return match.group("repo")
    return raw.strip("/")


def _default_repo() -> str:
    for key in ("SELF_IMPROVE_REPO", "GITHUB_REPOSITORY"):
        repo = _normalize_github_repo(os.getenv(key))
        if repo:
            return repo
    try:
        origin = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True,
        ).strip()
    except Exception:
        return ""
    return _normalize_github_repo(origin)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def extract_metadata(body: str) -> dict[str, Any] | None:
    if PR_METADATA_START not in body or PR_METADATA_END not in body:
        return None
    start = body.index(PR_METADATA_START) + len(PR_METADATA_START)
    end = body.index(PR_METADATA_END, start)
    raw = body[start:end].strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_thread_key(thread_key: str) -> tuple[str, str]:
    parts = thread_key.strip().split(":")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[1] and parts[2]:
        return parts[1], parts[2]
    raise ValueError(f"invalid thread key: {thread_key}")


def _extract_notifications(
    metadata: dict[str, Any],
    *,
    pr_number: int,
    pr_url: str,
    summary: str,
) -> list[dict[str, Any]]:
    notifications: list[dict[str, Any]] = []
    for thread in list(metadata.get("source_threads") or []):
        if not isinstance(thread, dict):
            continue
        thread_key = str(thread.get("thread_key") or "").strip()
        channel = str(thread.get("channel") or "").strip()
        thread_ts = str(thread.get("thread_ts") or "").strip()
        if not channel and not thread_ts and thread_key:
            try:
                channel, thread_ts = _parse_thread_key(thread_key)
            except ValueError:
                continue
        if not thread_key and channel and thread_ts:
            thread_key = f"{channel}:{thread_ts}"
        if not (thread_key and channel and thread_ts):
            continue
        notifications.append({
            "pr_number": pr_number,
            "pr_url": pr_url,
            "thread_key": thread_key,
            "channel": channel,
            "thread_ts": thread_ts,
            "summary": summary,
        })
    return notifications


def build_notifier_input(
    prs: list[dict[str, Any]],
    *,
    before_sha: str,
    after_sha: str,
    repo: str,
    deployed_at: str,
    baseline_sha: str,
    commit_is_ancestor: Callable[[str, str], bool],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "before_sha": before_sha,
        "after_sha": after_sha,
        "baseline_sha": baseline_sha,
        "repo": repo,
        "status": "success",
        "deployed_at": deployed_at,
        "merged_prs": [],
        "notifications": [],
    }

    merged_prs: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    for pr in prs:
        pr_number = _safe_int(pr.get("number"))
        pr_url = str(pr.get("url") or "").strip()
        merge_commit = pr.get("mergeCommit") if isinstance(pr, dict) else {}
        merge_oid = ""
        if isinstance(merge_commit, dict):
            merge_oid = str(merge_commit.get("oid") or "").strip()
        if not pr_number or not pr_url or not merge_oid:
            continue
        if not commit_is_ancestor(merge_oid, after_sha):
            continue
        if baseline_sha and commit_is_ancestor(merge_oid, baseline_sha):
            continue
        metadata = extract_metadata(str(pr.get("body") or ""))
        if not isinstance(metadata, dict):
            continue
        summary = str(
            metadata.get("summary") or f"Self-improvement PR #{pr_number}"
        ).strip()
        merged_prs.append({
            "pr_number": pr_number,
            "pr_url": pr_url,
            "summary": summary,
        })
        notifications.extend(
            _extract_notifications(
                metadata,
                pr_number=pr_number,
                pr_url=pr_url,
                summary=summary,
            )
        )

    payload["merged_prs"] = merged_prs
    payload["notifications"] = notifications
    return payload


def _gh_json(*args: str) -> Any:
    output = subprocess.check_output(["gh", *args], text=True)
    return json.loads(output)


def _api_json(path: str) -> Any:
    output = subprocess.check_output(
        [
            "docker",
            "exec",
            "centaur-api-1",
            "curl",
            "-s",
            f"http://localhost:8000{path}",
        ],
        text=True,
    )
    return json.loads(output)


def _git_stdout(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _commit_is_ancestor(commit_sha: str, after_sha: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit_sha, after_sha],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _resolve_baseline_sha(before_sha: str, after_sha: str) -> str:
    candidates: list[str] = []
    with contextlib.suppress(Exception):
        listing = _api_json(
            "/workflows/runs?workflow_name=self_improve_deploy_notifier&status=completed&limit=1"
        )
        items = listing.get("items") if isinstance(listing, dict) else []
        if items:
            run_id = str(items[0].get("run_id") or "").strip()
            if run_id:
                run = _api_json(f"/workflows/runs/{run_id}")
                output_json = run.get("output_json") if isinstance(run, dict) else {}
                if isinstance(output_json, dict):
                    candidates.append(str(output_json.get("after_sha") or "").strip())
    candidates.append(before_sha.strip())
    with contextlib.suppress(Exception):
        candidates.append(_git_stdout("rev-parse", f"{after_sha}^"))

    for candidate in candidates:
        if candidate and _commit_is_ancestor(candidate, after_sha):
            return candidate
    return after_sha


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-sha", default="")
    parser.add_argument("--after-sha", required=True)
    parser.add_argument("--repo", default=_default_repo())
    parser.add_argument("--deployed-at", required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    baseline_sha = _resolve_baseline_sha(args.before_sha, args.after_sha)

    prs = _gh_json(
        "pr",
        "list",
        "--repo",
        args.repo,
        "--state",
        "merged",
        "--label",
        "self-improve",
        "--limit",
        str(max(args.limit, 1)),
        "--json",
        "number,url,body,mergeCommit,mergedAt",
    )
    payload = build_notifier_input(
        prs if isinstance(prs, list) else [],
        before_sha=args.before_sha,
        after_sha=args.after_sha,
        repo=args.repo,
        deployed_at=args.deployed_at,
        baseline_sha=baseline_sha,
        commit_is_ancestor=_commit_is_ancestor,
    )
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
