"""Workflow: regenerate usage stats for the Centaur usage dashboard.

Queries agent_execution_events (Postgres) for tool/skill/user/team data
and nginx access logs (VictoriaLogs) for app traffic. Writes aggregated
stats to the usage_stats table as a single JSONB blob, with separate
aggregations per time window (all, 30d, 7d, 1d).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import Counter
from decimal import Decimal
from typing import Any

import httpx

from api.workflow_engine import WorkflowContext

log = logging.getLogger(__name__)

WORKFLOW_NAME = "usage_stats"
SCHEDULE = {"interval_seconds": 300, "no_delivery": True}

USER_MAP = {
    "U016MDTJM4J": ("Georgios Konstantopoulos", "georgios"),
    "U016V976J67": ("Jordan Qualls", "jordan"),
    "U01E7K6TC23": ("Lindsay Slocum", "lindsay"),
    "U01EG4BF4P7": ("Dan McCarthy", "dmccarthy"),
    "U02BCK5JFEG": ("Alex Ehlers", "alex"),
    "U02NBBUC6L8": ("Frankie xyz", "frankie"),
    "U02QKB5V6MC": ("Justin Slaughter", "justin"),
    "U03FGFTMG5A": ("Josie McGuinn", "josie"),
    "U03H4BH5AGL": ("Katie Biber", "katie"),
    "U03SDG6C6TC": ("Karina Berry", "karina"),
    "U03T8NE9JQH": ("Elena Page", "elena"),
    "U045TR9PGAZ": ("Brandon Wong", "brandon"),
    "U04889DSTEX": ("Dominique Little", "dominique"),
    "U04BL2T3D1S": ("Storm Slivkoff", "storm"),
    "U04BXMURWB0": ("Amy Sinclair", "amy"),
    "U04BY02ADG8": ("Alana Palmedo", "apalmedo"),
    "U05GY06RHMY": ("Alex Grieve", "agrieve"),
    "U05L1BZ4TJL": ("Chris Shu", "chris"),
    "U05P8C3NL0K": ("Tony Coppola", "tony"),
    "U05T7DMT84A": ("Vidhu Pinnamaraju", "vidhu"),
    "U076CL29AP5": ("David Swain", "david"),
    "U07F8P45GE4": ("Gracie Globerman", "gracie"),
    "U08ECTHML6T": ("Alpin Yukseloglu", "alpin"),
    "U08RSFNLG4W": ("Ricardo de Arruda", "ricardo"),
    "U08SGTSS5QS": ("Caleb Onofrei", "caleb"),
    "U08SPTN5Z36": ("Karina Ruiz Garcia", "kruizgarcia"),
    "U092QGQPD1N": ("Jordan Kong", "jkong"),
    "U0936H4LM8V": ("Veit Moeller", "veit"),
    "U094WMH3GPL": ("Chris Kraeuter", "ckraeuter"),
    "U09MYLH6K5F": ("Madison Parker", "madison"),
    "U09QAKUEHQB": ("Spencer Fluetsch", "spencer"),
    "U09TLJNR4PR": ("Ishan Goyal", "ishan"),
    "U0A1XRY62Q0": ("Trevor Holmgren", "trevor"),
    "U0A3UL147TN": ("Rama Somayajula", "rama"),
    "U0A43GX8Z8R": ("Asher Sedlin", "asher"),
    "U0A4UDV8VK2": ("Pam Tholen", "pam"),
    "U0A5X8LFC10": ("Chris Mann", "cmann"),
    "U0A88CAMB96": ("Stefan Schropp", "stefan"),
    "UGX1TH8TS": ("Dan Robinson", "dan"),
    "UGZCSQTPE": ("Matt Huang", "matt"),
    "UJ34LKUH0": ("Arjun Balaji", "arjun"),
    "UUR0BJKL7": ("Holly Morgan-Winsdale", "holly"),
    "U03059PNULX": ("Ben Hinshaw", "ben"),
    "U04851Q6TPF": ("Alex Popescu", "apopescu"),
    "U04URB3CX1B": ("Josh Waitzkin", "josh.waitzkin"),
    "U06K61QT4DB": ("Katie Shia", "kshia"),
    "U06T8RJ6B4M": ("Chentai Kao", "chentai"),
    "U087U0PNB2P": ("Nicki Lardieri", "nicki"),
    "U08NYJ0CFGW": ("Shogo Nakai", "shogo"),
    "U0924CKNXB5": ("Liz Khussein", "lkhussein"),
    "U0AQR2ABDHC": ("Flor Romero", "flor"),
}

TEAM_MAP = {
    "U016MDTJM4J": "I&R", "UGZCSQTPE": "I&R", "UGX1TH8TS": "I&R",
    "U02NBBUC6L8": "I&R", "U04BY02ADG8": "I&R", "UJ34LKUH0": "I&R",
    "U08ECTHML6T": "I&R", "U08RSFNLG4W": "I&R", "U04BL2T3D1S": "I&R",
    "U05L1BZ4TJL": "Talent", "U01EG4BF4P7": "Talent",
    "U01E7K6TC23": "Finance", "U0A4UDV8VK2": "Finance",
    "U016V976J67": "Finance", "U09QAKUEHQB": "Finance",
    "U0A43GX8Z8R": "Finance", "U05T7DMT84A": "Finance",
    "U08SGTSS5QS": "Finance", "U02BCK5JFEG": "Finance",
    "U0A3UL147TN": "Trading",
    "U092QGQPD1N": "Operations", "U0A1XRY62Q0": "Operations",
    "U09TLJNR4PR": "Operations",
    "U03H4BH5AGL": "Legal", "U0A88CAMB96": "Legal",
    "U04889DSTEX": "Policy", "U05GY06RHMY": "Policy",
    "U02QKB5V6MC": "Policy", "U09MYLH6K5F": "Policy",
    "U076CL29AP5": "Communications", "U094WMH3GPL": "Communications",
    "U0936H4LM8V": "Communications",
    "U03FGFTMG5A": "Events", "U05P8C3NL0K": "Events",
    "U08SPTN5Z36": "Events",
    "U03T8NE9JQH": "Admin", "U04BXMURWB0": "Admin",
    "U07F8P45GE4": "Admin", "U03SDG6C6TC": "Admin",
    "U045TR9PGAZ": "Engineering", "U0A5X8LFC10": "Engineering",
    "UUR0BJKL7": "Admin", "U03059PNULX": "Legal",
    "U04851Q6TPF": "Legal", "U04URB3CX1B": "Other",
    "U06K61QT4DB": "Engineering", "U06T8RJ6B4M": "Engineering",
    "U087U0PNB2P": "Admin", "U08NYJ0CFGW": "Engineering",
    "U0924CKNXB5": "Admin", "U0AQR2ABDHC": "Admin",
}

TEAM_EMOJIS = {
    "I&R": "\U0001F52C", "Finance": "\U0001F4B0", "Admin": "\U0001F4C5",
    "Policy": "\U0001F3DB", "Leadership": "\U0001F451",
    "Communications": "\U0001F4E2", "Legal": "\u2696\uFE0F",
    "Trading": "\U0001F4C8", "Events": "\U0001F389",
    "Engineering": "\U0001F527", "Talent": "\U0001F465",
    "Operations": "\u2699\uFE0F", "Centaur Internal": "\U0001F916",
    "Other": "\U0001F4AC",
}

SKILL_EMOJIS = {
    "gap-analysis": "\U0001F50D", "improve-gap-task": "\U0001F4A1",
    "policy-gigabrain": "\U0001F9E0", "building-skills": "\U0001F3D7",
    "sourcer": "\U0001F3AF", "learning-synthesis": "\U0001F4DA",
    "centaur-builder": "\U0001F6E0", "josh-thought-partner": "\U0001F4AC",
    "tldr": "\U0001F4CB", "creating-tools": "\U0001F527",
    "walkthrough": "\U0001F6B6", "talent-placement-updates": "\U0001F465",
    "qa": "\u2705", "term-sheet": "\U0001F4DD",
    "writing-friday-intl-slack": "\u270D\uFE0F",
    "archiving-thread-files-to-drive": "\U0001F4E6",
    "ship": "\U0001F680", "trade-approval": "\U0001F4B1",
    "preparing-monthly-trade-compliance": "\U0001F4CA",
    "slack-artifact-retrieval": "\U0001F50E", "gtm": "\U0001F4E3",
    "ir-companyprep": "\U0001F3E2", "researching-event-dates": "\U0001F4C5",
    "venue-scout": "\U0001F4CD",
}

APP_EMOJIS = {
    "docs": "\U0001F4D6", "usage": "\U0001F4CA",
    "hello-react": "\u269B\uFE0F", "tool-dashboard": "\U0001F4CA",
    "paradigm-sentiment-tracker": "\U0001F4C8",
    "block-metrics": "\u26D3\uFE0F", "dashboard": "\U0001F4CA",
    "test": "\U0001F9EA", "shift-timeline": "\U0001F4C5",
    "touchpoint-bot": "\U0001F916",
}

WORKFLOW_EMOJIS = {
    "slack_thread_turn": "\U0001F4AC",
    "policy_news_monitor": "\U0001F4F0",
    "self_improve_daily": "\U0001F504",
    "agent_turn": "\U0001F916",
    "paradigm_pulse_daily": "\U0001F4E1",
    "eth_morning_brief": "\u2615",
    "investment_pipeline_weekly": "\U0001F4BC",
    "daily_checklist_digest": "\u2705",
    "morning_market_brief": "\U0001F4C8",
    "room_conflict_monitor": "\U0001F3E2",
    "agent_loop": "\U0001F501",
    "usage_stats": "\U0001F4CA",
    "multi_step_demo": "\U0001F9EA",
    "self_improve_deploy_notifier": "\U0001F680",
}

NOISE_WORDS = frozenset({
    "it", "on", "in", "to", "at", "or", "an", "is", "be", "do",
    "him", "her", "out", "them", "through", "and", "parity", "tool",
    "ordering", "these", "this", "that",
})

INTERNAL_TOOLS = frozenset({
    "paradigmdb", "vlogs", "personas", "workflow", "agent", "investmemos",
    "research", "events", "termsheet", "social-monitor", "demo", "unit410",
    "infra", "crypto", "archiver", "media", "vmetrics", "metadata",
    "productivity", "comms", "nano-banana", "read_web_page",
})

STATIC_EXTS = frozenset({
    "css", "js", "png", "ico", "json", "svg", "jpg",
    "woff", "woff2", "map", "webp",
})

CALL_RE = re.compile(r"call\s+([a-z][a-z0-9_-]*)\s+([a-z][a-z0-9_-]*)")
CURL_RE = re.compile(r"/tools/([a-z][a-z0-9_-]+)/([a-z][a-z0-9_-]+)")

WINDOWS = {"all": None, "30d": 30, "7d": 7, "1d": 1}


def _is_slack_thread(thread_key: str) -> bool:
    return ":" in thread_key and thread_key.split(":", 1)[0].startswith(("C", "D", "G"))


def _session_key(thread_key: str) -> str:
    if _is_slack_thread(thread_key):
        return thread_key
    if thread_key.startswith("workflow:wfr_") and thread_key.count(":") >= 2:
        parts = thread_key.split(":", 2)
        return f"{parts[0]}:{parts[1]}"
    return re.sub(r"-\d{10,}$", "", thread_key)


def _thread_to_slack_url(thread_key: str) -> str | None:
    if ":" not in thread_key:
        return None
    channel, ts = thread_key.split(":", 1)
    if not channel.startswith(("C", "D", "G")):
        return None
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _parse_tool_call(cmd: str) -> tuple[str, str] | None:
    m = CALL_RE.match(cmd)
    if m and m.group(1) != "discover":
        return m.group(1), m.group(2)
    m = CURL_RE.search(cmd)
    if m:
        return m.group(1), m.group(2)
    return None


def _fmt_user(uid: str, cnt: int) -> str:
    if uid == "unknown":
        return f"Centaur Internal ({cnt})"
    name = USER_MAP.get(uid, (uid,))[0]
    short = name.split()[0] if " " in name else name
    return f"{short} ({cnt})"


async def _get_thread_users(pool) -> dict[str, str]:
    rows = await pool.fetch(
        "SELECT thread_key, user_id FROM ("
        "  SELECT thread_key, user_id,"
        "    ROW_NUMBER() OVER (PARTITION BY thread_key ORDER BY created_at) as rn"
        "  FROM chat_messages WHERE role = 'user' AND user_id IS NOT NULL"
        ") sub WHERE rn = 1"
    )
    return {r["thread_key"]: r["user_id"] for r in rows}


# ── Raw event parsing (query once, aggregate per window) ─────────


async def _fetch_tool_events(pool, thread_users: dict[str, str]) -> list[dict]:
    """Fetch and parse all shell_command events into flat records."""
    rows = await pool.fetch(
        "SELECT thread_key, created_at, event_json::text as ej "
        "FROM agent_execution_events "
        "WHERE event_kind = 'amp_raw_event' "
        "  AND event_json->>'type' = 'assistant' "
        "  AND event_json::text LIKE '%shell_command%'"
        "  AND created_at > NOW() - INTERVAL '90 days'"
    )
    events = []
    for row in rows:
        ej = json.loads(row["ej"])
        for block in ej.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "shell_command":
                continue
            parsed = _parse_tool_call(block.get("input", {}).get("command", ""))
            if not parsed or parsed[0] in NOISE_WORDS:
                continue
            events.append({
                "tool": parsed[0],
                "method": parsed[1],
                "uid": thread_users.get(row["thread_key"], "unknown"),
                "thread": row["thread_key"],
                "day": row["created_at"].strftime("%Y-%m-%d"),
                "ts": row["created_at"].isoformat(),
            })
    return events


async def _fetch_skill_events(pool, thread_users: dict[str, str]) -> list[dict]:
    rows = await pool.fetch(
        "SELECT e.thread_key, e.created_at, "
        "  elem->'input'->>'name' as skill_name "
        "FROM agent_execution_events e, "
        "  jsonb_array_elements(e.event_json->'message'->'content') elem "
        "WHERE e.event_kind = 'amp_raw_event' "
        "  AND e.event_json->>'type' = 'assistant' "
        "  AND elem->>'type' = 'tool_use' "
        "  AND elem->>'name' = 'skill' "
        "  AND elem->'input'->>'name' IS NOT NULL"
        "  AND e.created_at > NOW() - INTERVAL '90 days'"
    )
    return [{
        "skill": row["skill_name"],
        "uid": thread_users.get(row["thread_key"], "unknown"),
        "thread": row["thread_key"],
        "day": row["created_at"].strftime("%Y-%m-%d"),
        "ts": row["created_at"].isoformat(),
    } for row in rows]


async def _fetch_workflow_events(pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT workflow_name, status, "
        "  created_at, started_at, completed_at "
        "FROM workflow_runs "
        "WHERE created_at > NOW() - INTERVAL '90 days'"
    )
    return [{
        "workflow": row["workflow_name"],
        "status": row["status"],
        "ts": row["created_at"].isoformat(),
        "day": row["created_at"].strftime("%Y-%m-%d"),
        "duration_s": (
            float((row["completed_at"] - row["started_at"]).total_seconds())
            if row["completed_at"] and row["started_at"] else None
        ),
    } for row in rows]


async def _fetch_token_events(pool, thread_users: dict[str, str]) -> list[dict]:
    rows = await pool.fetch(
        "SELECT thread_key, created_at, "
        "  (event_json->>'total_tokens')::bigint as tokens "
        "FROM agent_execution_events "
        "WHERE event_kind = 'execution_summary' "
        "  AND created_at > NOW() - INTERVAL '90 days' "
        "  AND (event_json->>'total_tokens')::bigint > 0"
    )
    return [{
        "uid": thread_users.get(row["thread_key"], "unknown"),
        "tokens": row["tokens"],
        "ts": row["created_at"].isoformat(),
    } for row in rows]


# ── Aggregation (runs per window on pre-fetched events) ──────────


def _aggregate_tools(events: list[dict]) -> list[dict]:
    stats: dict[str, dict] = {}
    for e in events:
        tool = e["tool"]
        if tool not in stats:
            stats[tool] = {
                "count": 0, "threads": set(), "users": Counter(),
                "methods": Counter(), "first": "9999", "last": "0000",
                "first_thread": None, "last_thread": None,
            }
        s = stats[tool]
        s["count"] += 1
        s["threads"].add(_session_key(e["thread"]))
        s["users"][e["uid"]] += 1
        s["methods"][e["method"]] += 1
        if e["day"] < s["first"]:
            s["first"] = e["day"]
            s["first_thread"] = e["thread"]
        if e["day"] >= s["last"]:
            s["last"] = e["day"]
            s["last_thread"] = e["thread"]

    result = []
    for tool in sorted(stats, key=lambda t: len(stats[t]["threads"]), reverse=True):
        s = stats[tool]
        top = s["methods"].most_common(3)
        result.append({
            "tool": tool,
            "calls": s["count"],
            "threads": len(s["threads"]),
            "users": len(s["users"]),
            "methods_count": len(s["methods"]),
            "calls_per_thread": round(s["count"] / len(s["threads"]), 1),
            "method1": f"{top[0][0]} ({top[0][1]:,})" if len(top) > 0 else "",
            "method2": f"{top[1][0]} ({top[1][1]:,})" if len(top) > 1 else "",
            "method3": f"{top[2][0]} ({top[2][1]:,})" if len(top) > 2 else "",
            "first_seen": s["first"],
            "last_seen": s["last"],
            "first_url": _thread_to_slack_url(s["first_thread"] or ""),
            "last_url": _thread_to_slack_url(s["last_thread"] or ""),
            "icon": "centaur.png" if tool in INTERNAL_TOOLS else f"icons/{tool}.png",
        })
    return result


def _aggregate_users(events: list[dict], token_events: list[dict] | None = None) -> list[dict]:
    stats: dict[str, dict] = {}
    for e in events:
        uid = e["uid"]
        if uid not in stats:
            stats[uid] = {"count": 0, "threads": set(), "tools": set(), "top_tools": Counter()}
        s = stats[uid]
        s["count"] += 1
        s["threads"].add(_session_key(e["thread"]))
        s["tools"].add(e["tool"])
        s["top_tools"][e["tool"]] += 1

    user_tokens: dict[str, int] = {}
    for te in (token_events or []):
        user_tokens[te["uid"]] = user_tokens.get(te["uid"], 0) + te["tokens"]
    result = []
    for uid in sorted(stats, key=lambda u: stats[u]["count"], reverse=True):
        s = stats[uid]
        name_handle = USER_MAP.get(uid, ("Centaur Internal", "\u2014") if uid == "unknown" else (uid, uid))
        name, handle = name_handle
        team = TEAM_MAP.get(uid, "Centaur Internal" if uid == "unknown" else "Other")
        top = s["top_tools"].most_common(3)
        result.append({
            "name": name, "handle": handle, "team": team,
            "team_emoji": TEAM_EMOJIS.get(team, ""),
            "calls": s["count"],
            "threads": len(s["threads"]),
            "tokens": user_tokens.get(uid, 0),
            "tools": len(s["tools"]),
            "calls_per_thread": round(s["count"] / len(s["threads"]), 1),
            "tool1": f"{top[0][0]} ({top[0][1]:,})" if len(top) > 0 else "",
            "tool2": f"{top[1][0]} ({top[1][1]:,})" if len(top) > 1 else "",
            "tool3": f"{top[2][0]} ({top[2][1]:,})" if len(top) > 2 else "",
        })
    return result


def _aggregate_skills(events: list[dict]) -> list[dict]:
    stats: dict[str, dict] = {}
    for e in events:
        skill = e["skill"]
        if skill not in stats:
            stats[skill] = {
                "count": 0, "threads": set(), "users": Counter(),
                "first": "9999", "last": "0000",
                "first_thread": None, "last_thread": None,
            }
        s = stats[skill]
        s["count"] += 1
        s["threads"].add(_session_key(e["thread"]))
        s["users"][e["uid"]] += 1
        if e["day"] < s["first"]:
            s["first"] = e["day"]
            s["first_thread"] = e["thread"]
        if e["day"] >= s["last"]:
            s["last"] = e["day"]
            s["last_thread"] = e["thread"]

    result = []
    for skill in sorted(stats, key=lambda k: len(stats[k]["threads"]), reverse=True):
        s = stats[skill]
        top_users = s["users"].most_common(3)
        result.append({
            "skill": skill,
            "calls": s["count"],
            "threads": len(s["threads"]),
            "users": len(s["users"]),
            "calls_per_thread": round(s["count"] / len(s["threads"]), 1),
            "user1": _fmt_user(*top_users[0]) if len(top_users) > 0 else "",
            "user2": _fmt_user(*top_users[1]) if len(top_users) > 1 else "",
            "user3": _fmt_user(*top_users[2]) if len(top_users) > 2 else "",
            "first_seen": s["first"],
            "last_seen": s["last"],
            "first_url": _thread_to_slack_url(s["first_thread"] or ""),
            "last_url": _thread_to_slack_url(s["last_thread"] or ""),
            "emoji": SKILL_EMOJIS.get(skill, "\U0001F4AC"),
        })
    return result


def _build_teams(user_rows: list[dict]) -> list[dict]:
    teams: dict[str, dict] = {}
    for u in user_rows:
        t = u["team"]
        if t not in teams:
            teams[t] = {"calls": 0, "threads_sum": 0, "members": []}
        teams[t]["calls"] += u["calls"]
        teams[t]["threads_sum"] += u["threads"]
        teams[t]["members"].append(u["name"])

    result = []
    for name in sorted(teams, key=lambda k: teams[k]["calls"], reverse=True):
        t = teams[name]
        n = len(t["members"])
        result.append({
            "team": name, "members": n,
            "calls": t["calls"], "threads": t["threads_sum"],
            "calls_per_member": round(t["calls"] / n, 1) if n else 0,
            "threads_per_member": round(t["threads_sum"] / n, 1) if n else 0,
            "member_list": ", ".join(sorted(t["members"])),
            "emoji": TEAM_EMOJIS.get(name, ""),
        })
    return result


def _aggregate_workflows(events: list[dict]) -> list[dict]:
    stats: dict[str, dict] = {}
    for e in events:
        name = e["workflow"]
        if name not in stats:
            stats[name] = {
                "completed": 0, "failed": 0, "other": 0, "total": 0,
                "first": None, "last": None, "durations": [],
            }
        s = stats[name]
        s["total"] += 1
        if e["status"] == "completed":
            s["completed"] += 1
            if e["duration_s"] is not None:
                s["durations"].append(e["duration_s"])
        elif e["status"] == "failed":
            s["failed"] += 1
        else:
            s["other"] += 1
        if not s["first"] or e["day"] < s["first"]:
            s["first"] = e["day"]
        if not s["last"] or e["day"] >= s["last"]:
            s["last"] = e["day"]

    result = []
    for name in sorted(stats, key=lambda n: stats[n]["total"], reverse=True):
        s = stats[name]
        avg_d = round(sum(s["durations"]) / len(s["durations"]), 1) if s["durations"] else None
        result.append({
            "workflow": name,
            "total": s["total"],
            "completed": s["completed"],
            "failed": s["failed"],
            "other": s["other"],
            "success_rate": round(s["completed"] / s["total"] * 100, 1) if s["total"] > 0 else 0,
            "avg_duration_s": avg_d,
            "first_seen": s["first"] or "",
            "last_seen": s["last"] or "",
            "emoji": WORKFLOW_EMOJIS.get(name, "\u2699\uFE0F"),
        })
    return result


# ── VictoriaLogs (app traffic) ───────────────────────────────────


async def _vlogs_stats(client: httpx.AsyncClient, query: str) -> dict[str, int]:
    resp = await client.get(
        "http://victorialogs:9428/select/logsql/stats_query",
        params={"query": query},
    )
    resp.raise_for_status()
    data = resp.json()
    result: dict[str, int] = {}
    for item in data.get("data", {}).get("result", []):
        metric = item.get("metric", {})
        label = ""
        for k, v in metric.items():
            if k != "__name__":
                label = v
                break
        value = int(item.get("value", [0, "0"])[1])
        if label and " " not in label and "?" not in label and '"' not in label:
            result[label] = result.get(label, 0) + value
    return result


async def _extract_apps(pool, window_label: str) -> list[dict]:
    app_rows = await pool.fetch("SELECT name, status FROM apps ORDER BY name")
    app_status = {r["name"]: r["status"] for r in app_rows}

    time_filter = "_time:1y" if window_label == "all" else f"_time:{window_label}"
    views: dict[str, int] = {}
    requests: dict[str, int] = {}
    visitors: dict[str, int] = {}
    errors: dict[str, int] = {}

    static_filter = " AND NOT _msg:.css AND NOT _msg:.js AND NOT _msg:.png AND NOT _msg:.ico AND NOT _msg:.json AND NOT _msg:.svg AND NOT _msg:.woff AND NOT _msg:.map AND NOT _msg:.webp"
    base = f'{time_filter} AND _msg:/apps/ AND _msg:"HTTP/1.1" AND NOT _msg:INFO'
    extract_app = '| extract "GET /apps/<app>/" from _msg'
    extract_app_any = '| extract "/apps/<app>/" from _msg'
    extract_ip = '| extract "\\"<ip>\\"" from _msg'

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            views = await _vlogs_stats(
                client,
                f'{base} AND _msg:"GET /apps/" AND _msg:" 200 "{static_filter} {extract_app} | stats by (app) count() as views',
            )
            requests = await _vlogs_stats(
                client,
                f"{base} {extract_app_any} | stats by (app) count() as requests",
            )
            visitors = await _vlogs_stats(
                client,
                f"{base} {extract_app_any} {extract_ip} | stats by (app) count_uniq(ip) as visitors",
            )
            errors = await _vlogs_stats(
                client,
                f'{base} AND _msg:" 5" {extract_app_any} | stats by (app) count() as errors',
            )
    except Exception:
        log.warning("failed to query VictoriaLogs for app traffic", exc_info=True)

    all_apps = set(views) | set(requests) | set(visitors)
    result = []
    for app in sorted(all_apps, key=lambda a: views.get(a, 0), reverse=True):
        v = views.get(app, 0)
        r = requests.get(app, 0)
        if v == 0 and r < 3:
            continue
        result.append({
            "app": app,
            "views": v, "requests": r,
            "visitors": visitors.get(app, 0),
            "errors": errors.get(app, 0),
            "error_rate": round(errors.get(app, 0) / r * 100, 1) if r > 0 else 0,
            "status": app_status.get(app, "?"),
            "emoji": APP_EMOJIS.get(app, "\U0001F4E6"),
        })
    return result


# ── Window filtering ─────────────────────────────────────────────


def _filter_by_window(events: list[dict], days: int | None) -> list[dict]:
    if days is None:
        return events
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    return [e for e in events if e["ts"] >= cutoff]


def _build_window(
    tool_events: list[dict],
    skill_events: list[dict],
    workflow_events: list[dict],
    days: int | None,
    token_events: list[dict] | None = None,
) -> dict[str, list[dict]]:
    te = _filter_by_window(tool_events, days)
    se = _filter_by_window(skill_events, days)
    we = _filter_by_window(workflow_events, days)
    tke = _filter_by_window(token_events or [], days)

    tools = _aggregate_tools(te)
    users = _aggregate_users(te, tke)
    skills = _aggregate_skills(se)
    teams = _build_teams(users)
    workflows = _aggregate_workflows(we)

    return {
        "tools": tools,
        "skills": skills,
        "users": users,
        "teams": teams,
        "workflows": workflows,
    }


# ── Handler ──────────────────────────────────────────────────────


async def handler(_inp: dict[str, Any], ctx: WorkflowContext) -> dict[str, Any]:
    pool = ctx._pool

    thread_users = await ctx.step(
        "get_thread_users",
        lambda: _get_thread_users(pool),
        step_kind="gather",
    )

    tool_events = await ctx.step(
        "fetch_tool_events",
        lambda: _fetch_tool_events(pool, thread_users),
        step_kind="gather",
    )

    skill_events = await ctx.step(
        "fetch_skill_events",
        lambda: _fetch_skill_events(pool, thread_users),
        step_kind="gather",
    )

    workflow_events = await ctx.step(
        "fetch_workflow_events",
        lambda: _fetch_workflow_events(pool),
        step_kind="gather",
    )

    token_events = await ctx.step(
        "fetch_token_events",
        lambda: _fetch_token_events(pool, thread_users),
        step_kind="gather",
    )

    windowed = {}
    for label, days in WINDOWS.items():
        windowed[label] = await ctx.step(
            f"build_window_{label}",
            lambda d=days: _build_window(tool_events, skill_events, workflow_events, d, token_events),
            step_kind="transform",
        )

    # Apps use VictoriaLogs with its own time filter
    for label in WINDOWS:
        windowed[label]["apps"] = await ctx.step(
            f"extract_apps_{label}",
            lambda lbl=label: _extract_apps(pool, lbl),
            step_kind="gather",
        )

    data = {"windows": windowed}

    await ctx.step(
        "write_stats",
        lambda: pool.execute(
            "INSERT INTO usage_stats (id, data_json, generated_at) "
            "VALUES ('current', $1::jsonb, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET data_json = $1::jsonb, generated_at = NOW()",
            json.dumps(data, default=lambda o: float(o) if isinstance(o, Decimal) else str(o)),
        ),
        step_kind="persist",
    )

    all_w = windowed.get("all", {})
    ctx.log(
        "usage_stats_generated",
        tools=len(all_w.get("tools", [])),
        skills=len(all_w.get("skills", [])),
        users=len(all_w.get("users", [])),
        teams=len(all_w.get("teams", [])),
        apps=len(all_w.get("apps", [])),
        workflows=len(all_w.get("workflows", [])),
    )

    return {
        "status": "ok",
        "windows": {k: {dk: len(dv) for dk, dv in v.items()} for k, v in windowed.items()},
    }
