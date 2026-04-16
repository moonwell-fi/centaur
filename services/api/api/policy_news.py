from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import asyncpg
import httpx
from anthropic import Anthropic

from api.workflow_engine import WorkflowContext

TOPIC_ORDER = ["Crypto", "AI", "Prediction Markets", "Defense Tech"]
DELIVERY_ORDER = {"Urgent": 0, "Standard": 1, "Narrative": 2, "Archive Only": 3}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "for",
    "from",
    "has",
    "in",
    "into",
    "its",
    "of",
    "on",
    "or",
    "over",
    "the",
    "to",
    "with",
}
FEEDBACK_PREFIXES = [
    "good catch",
    "false positive",
    "too low priority",
    "should have been urgent",
    "wrong topic",
    "more like this",
    "less like this",
]
QUERY_PREFIXES = ("search ", "show ", "find ", "what did we send")
PRIORITY_AGENCIES = ["SEC", "CFTC", "OCC", "FinCEN", "OFAC", "Treasury"]
WATCH_TERMS = [
    ("GENIUS Act", "crypto", 5),
    ("CLARITY Act", "crypto", 5),
    ("stablecoin", "crypto", 3),
    ("DeFi", "crypto", 3),
    ("digital assets", "crypto", 4),
    ("crypto regulation", "crypto", 4),
    ("SEC crypto", "crypto", 3),
    ("CFTC crypto", "crypto", 3),
    ("market structure", "crypto", 4),
    ("crypto tax", "crypto", 3),
    ("AI regulation", "ai", 4),
    ("artificial intelligence policy", "ai", 4),
    ("AI governance", "ai", 4),
    ("AI Act", "ai", 3),
    ("foundation models", "ai", 4),
    ("AI safety", "ai", 4),
    ("algorithmic accountability", "ai", 4),
    ("prediction markets", "prediction", 4),
    ("Kalshi", "prediction", 4),
    ("Polymarket", "prediction", 4),
    ("CFTC prediction", "prediction", 4),
    ("event contracts", "prediction", 4),
    ("Senate Banking", "congress", 5),
    ("House Financial Services", "congress", 5),
    ("Senate Agriculture", "congress", 5),
    ("HFSC", "congress", 5),
    ("markup", "congress", 5),
    ("floor vote", "congress", 5),
    ("reconciliation", "congress", 4),
    ("DNC", "politics", 2),
    ("Senate Democrats", "politics", 2),
    ("House Democrats", "politics", 2),
    ("DSCC", "politics", 2),
    ("DCCC", "politics", 2),
    ("Democratic caucus", "politics", 2),
]
DEFAULT_SOURCE_META: dict[str, dict[str, Any]] = {
    "Politico": {"trust_tier": 5},
    "The Hill": {"trust_tier": 4},
    "Punchbowl News": {"trust_tier": 5},
    "Axios": {"trust_tier": 5},
    "Bloomberg News": {"trust_tier": 5},
    "Bloomberg Government": {"trust_tier": 5},
    "Semafor": {"trust_tier": 4},
    "CoinDesk": {"trust_tier": 3, "topic_hints": ["Crypto"]},
    "The Block": {"trust_tier": 3, "topic_hints": ["Crypto"]},
    "The Information": {"trust_tier": 4},
    "Wired": {"trust_tier": 4},
    "Breaking Defense": {"trust_tier": 4, "topic_hints": ["Defense Tech"]},
    "Defense One": {"trust_tier": 4, "topic_hints": ["Defense Tech"]},
    "Defense News": {"trust_tier": 4, "topic_hints": ["Defense Tech"]},
    "Reuters": {"trust_tier": 5},
    "Washington Post": {"trust_tier": 5},
    "New York Times": {"trust_tier": 5},
    "Associated Press": {"trust_tier": 5},
    "Wall Street Journal": {"trust_tier": 5},
    "Financial Times": {"trust_tier": 5},
    "Roll Call": {"trust_tier": 5},
    "CQ": {"trust_tier": 5},
    "Law360": {"trust_tier": 5},
    "Nextgov/FCW": {"trust_tier": 4, "topic_hints": ["AI", "Defense Tech"]},
}
TOPIC_KEYWORDS = {
    "Crypto": [
        "bitcoin",
        "crypto",
        "cryptocurrency",
        "stablecoin",
        "digital asset",
        "defi",
        "token",
        "market structure",
        "clarity act",
        "genius act",
        "sec crypto",
        "cftc crypto",
    ],
    "AI": [
        "artificial intelligence",
        " ai ",
        "algorithmic",
        "foundation model",
        "frontier model",
        "ai safety",
        "ai governance",
        "ai regulation",
    ],
    "Prediction Markets": [
        "prediction market",
        "prediction markets",
        "kalshi",
        "polymarket",
        "event contract",
        "event contracts",
    ],
    "Defense Tech": [
        "defense tech",
        "pentagon",
        "dod",
        "department of defense",
        "procurement",
        "autonomy",
        "drone",
        "export control",
        "munitions",
        "shipbuilding",
        "warfighter",
    ],
}
CONGRESS_TERMS = [
    "senate",
    "house",
    "committee",
    "markup",
    "hearing",
    "floor vote",
    "hfsc",
    "senate banking",
    "house financial services",
    "senate agriculture",
    "ranking member",
    "chairman",
    "chairwoman",
    "congress",
]
AGENCY_TERMS = [
    "sec",
    "cftc",
    "occ",
    "fincen",
    "ofac",
    "treasury",
    "federal register",
    "department of justice",
    "doj",
    "fcc",
    "commerce department",
]
ENFORCEMENT_TERMS = [
    "lawsuit",
    "settlement",
    "enforcement",
    "charges",
    "indictment",
    "consent order",
    "sues",
    "investigation",
]
MARKUP_TERMS = ["markup", "floor vote", "reconciliation", "hearing"]
OPINION_TERMS = ["opinion", "editorial", "analysis", "guest essay", "commentary"]
LIVE_UPDATE_TERMS = ["live updates", "live update", "live blog"]
URGENT_TERMS = MARKUP_TERMS + [
    "final rule",
    "rulemaking",
    "executive order",
    "court rules",
    "awards contract",
    "wins contract",
]
NOISE_TERMS = [
    "raises",
    "funding",
    "series a",
    "series b",
    "launches",
    "launch",
    "unveils",
    "partnership",
    "conference",
    "summit",
]
DEFAULT_POLICY_NEWS_FEEDS_FILE = "/app/workflows/policy_news_sources.json"


@dataclass
class MonitorInput:
    slack_channel: str = ""
    feeds_file: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    process_replies: bool = True
    max_articles_per_feed: int = 40
    max_candidates: int = 40
    max_alerts_per_run: int = 8
    max_query_results: int = 8
    classifier_provider: str = "auto"
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceConfig:
    name: str
    feed_url: str
    source_kind: str = "news"
    trust_tier: int = 3
    topic_hints: list[str] = field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source_key(self) -> str:
        return slugify(self.name)


@dataclass
class MonitorConfig:
    slack_channel: str
    sources: list[SourceConfig]
    dry_run: bool = False
    process_replies: bool = True
    max_articles_per_feed: int = 40
    max_candidates: int = 40
    max_alerts_per_run: int = 8
    max_query_results: int = 8
    classifier_provider: str = "auto"
    model: str = ""


@dataclass
class ArticleCandidate:
    article_key: str
    source_key: str
    source_name: str
    source_kind: str
    trust_tier: int
    title: str
    title_normalized: str
    canonical_url: str
    excerpt: str
    content_text: str
    author: str
    published_at: dt.datetime | None
    categories: list[str]
    excerpt_only: bool
    topic_hints: list[str]


@dataclass
class ClassificationResult:
    article_key: str
    primary_topic: str = ""
    secondary_tags: list[str] = field(default_factory=list)
    include: bool = False
    reason_for_inclusion: str = ""
    what_happened: str = ""
    why_it_matters: str = ""
    classifier_notes: str = ""
    suggested_delivery: str = "Archive Only"
    scores: dict[str, int] = field(default_factory=dict)
    exclusion_reason: str = ""

    @property
    def total_score(self) -> int:
        return sum(int(v or 0) for v in self.scores.values())


@dataclass
class FeedbackCommand:
    command: str
    note: str = ""


@dataclass
class QueryRequest:
    raw_text: str
    sent_only: bool = False
    topic: str = ""
    source_names: list[str] = field(default_factory=list)
    delivery_class: str = ""
    since: dt.datetime | None = None
    search_text: str = ""


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "source"


def lookup_source_defaults(name: str) -> dict[str, Any]:
    exact = DEFAULT_SOURCE_META.get(name)
    if exact is not None:
        return exact
    base = re.split(r"\s+-\s+", name, maxsplit=1)[0].strip()
    return DEFAULT_SOURCE_META.get(base, {})


def normalize_slack_channel(value: str) -> str:
    channel = value.strip()
    if re.fullmatch(r"#[CG][A-Z0-9]+", channel):
        return channel[1:]
    return channel


def _coerce_input(inp: Any) -> MonitorInput:
    allowed_fields = {field_info.name for field_info in fields(MonitorInput)}
    if isinstance(inp, MonitorInput):
        return inp
    if is_dataclass(inp):
        raw = asdict(inp)
        return MonitorInput(
            **{key: value for key, value in raw.items() if key in allowed_fields}
        )
    if isinstance(inp, dict):
        return MonitorInput(
            **{key: value for key, value in inp.items() if key in allowed_fields}
        )
    return MonitorInput()


def load_monitor_config(raw_input: Any) -> MonitorConfig:
    inp = _coerce_input(raw_input)
    file_sources: list[dict[str, Any]] = []
    file_channel = ""
    feeds_file = (
        inp.feeds_file.strip()
        or os.getenv("POLICY_NEWS_FEEDS_FILE", "").strip()
        or DEFAULT_POLICY_NEWS_FEEDS_FILE
    )
    if feeds_file:
        data = json.loads(Path(feeds_file).read_text())
        if isinstance(data, dict):
            file_sources = list(data.get("sources") or [])
            file_channel = str(data.get("slack_channel") or "").strip()
    raw_sources = inp.sources or file_sources
    sources: list[SourceConfig] = []
    for raw_source in raw_sources:
        name = str(raw_source.get("name") or "").strip()
        feed_url = str(raw_source.get("feed_url") or raw_source.get("url") or "").strip()
        if not name or not feed_url:
            continue
        defaults = lookup_source_defaults(name)
        source = SourceConfig(
            name=name,
            feed_url=feed_url,
            source_kind=str(
                raw_source.get("source_kind") or defaults.get("source_kind") or "news"
            ),
            trust_tier=int(
                raw_source.get("trust_tier") or defaults.get("trust_tier") or 3
            ),
            topic_hints=list(
                raw_source.get("topic_hints") or defaults.get("topic_hints") or []
            ),
            enabled=bool(raw_source.get("enabled", True)),
            metadata=dict(raw_source.get("metadata") or {}),
        )
        if source.enabled:
            sources.append(source)
    return MonitorConfig(
        slack_channel=normalize_slack_channel(
            inp.slack_channel.strip()
            or file_channel
            or os.getenv("POLICY_NEWS_SLACK_CHANNEL", "").strip()
        ),
        sources=sources,
        dry_run=inp.dry_run,
        process_replies=inp.process_replies,
        max_articles_per_feed=max(inp.max_articles_per_feed, 1),
        max_candidates=max(inp.max_candidates, 1),
        max_alerts_per_run=max(inp.max_alerts_per_run, 1),
        max_query_results=max(inp.max_query_results, 1),
        classifier_provider=inp.classifier_provider.strip() or "auto",
        model=inp.model.strip(),
    )


async def run_policy_news_monitor(
    ctx: WorkflowContext,
    raw_input: Any,
) -> dict[str, Any]:
    config = load_monitor_config(raw_input)
    if not config.sources:
        return {"status": "noop", "reason": "no configured sources"}
    await sync_config(ctx._pool, config)
    stats = {
        "sources": len(config.sources),
        "fetch_successes": 0,
        "fetch_failures": 0,
        "new_articles": 0,
        "classified": 0,
        "alerts_sent": 0,
        "feedback_commands": 0,
        "query_replies": 0,
        "dry_run_preview": [],
    }
    for source in config.sources:
        try:
            items = await fetch_feed(source, limit=config.max_articles_per_feed)
            inserted = await store_articles(ctx._pool, source, items)
            await record_fetch(ctx._pool, source.source_key, "ok", len(items), "")
            stats["fetch_successes"] += 1
            stats["new_articles"] += len(inserted)
        except Exception as exc:
            await record_fetch(ctx._pool, source.source_key, "error", 0, str(exc)[:500])
            stats["fetch_failures"] += 1
    candidates = await list_unprocessed_articles(ctx._pool, limit=config.max_candidates)
    if candidates:
        classifications = await classify_candidates(config, candidates)
        stats["classified"] = len(classifications)
        ready_clusters = await upsert_clusters(ctx._pool, candidates, classifications)
        alertable = [c for c in ready_clusters if c["delivery_class"] != "Archive Only"]
        alertable.sort(
            key=lambda item: (
                DELIVERY_ORDER.get(str(item["delivery_class"]), 99),
                -int(item["score_total"]),
                str(item["canonical_title"]),
            )
        )
        for cluster in alertable[: config.max_alerts_per_run]:
            message = await build_alert_message(ctx._pool, cluster)
            if config.dry_run:
                stats["dry_run_preview"].append(message)
                continue
            response = await ctx.post_to_slack(config.slack_channel, message)
            await record_alert(
                ctx._pool,
                cluster_id=str(cluster["cluster_id"]),
                channel_id=str(response.get("channel") or config.slack_channel),
                thread_ts=str(response.get("ts") or ""),
                delivery_class=str(cluster["delivery_class"]),
                message_text=message,
                reason_for_inclusion=str(cluster["reason_for_inclusion"]),
                score_total=int(cluster["score_total"]),
            )
            await ctx._pool.execute(
                "UPDATE policy_news_clusters SET last_alerted_at = NOW(), updated_at = NOW() "
                "WHERE cluster_id = $1",
                str(cluster["cluster_id"]),
            )
            stats["alerts_sent"] += 1
    if config.process_replies and not config.dry_run:
        feedback_count, reply_count = await process_alert_replies(ctx, config)
        stats["feedback_commands"] = feedback_count
        stats["query_replies"] = reply_count
    return {"status": "ok", **stats}


async def sync_config(pool: asyncpg.Pool, config: MonitorConfig) -> None:
    async with pool.acquire() as conn:
        for source in config.sources:
            await conn.execute(
                "INSERT INTO policy_news_sources ("
                "source_key, name, feed_url, source_kind, trust_tier, topic_hints, enabled, metadata"
                ") VALUES ($1, $2, $3, $4, $5, $6::jsonb, TRUE, $7::jsonb) "
                "ON CONFLICT (source_key) DO UPDATE SET "
                "name = EXCLUDED.name, feed_url = EXCLUDED.feed_url, "
                "source_kind = EXCLUDED.source_kind, trust_tier = EXCLUDED.trust_tier, "
                "topic_hints = EXCLUDED.topic_hints, enabled = TRUE, metadata = EXCLUDED.metadata, "
                "updated_at = NOW()",
                source.source_key,
                source.name,
                source.feed_url,
                source.source_kind,
                source.trust_tier,
                json.dumps(source.topic_hints),
                json.dumps(source.metadata),
            )
        for term, category, boost in WATCH_TERMS:
            await conn.execute(
                "INSERT INTO policy_news_watch_terms (term, category, boost) VALUES ($1, $2, $3) "
                "ON CONFLICT (term) DO UPDATE SET category = EXCLUDED.category, "
                "boost = EXCLUDED.boost, updated_at = NOW()",
                term,
                category,
                boost,
            )


async def record_fetch(
    pool: asyncpg.Pool,
    source_key: str,
    status: str,
    item_count: int,
    error_text: str,
) -> None:
    await pool.execute(
        "INSERT INTO policy_news_feed_fetches (source_key, status, item_count, error_text) "
        "VALUES ($1, $2, $3, $4)",
        source_key,
        status,
        item_count,
        error_text,
    )


async def fetch_feed(source: SourceConfig, *, limit: int) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": "CentaurPolicyNewsMonitor/1.0 (+https://github.com/paradigmxyz/centaur)",
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(source.feed_url, headers=headers)
        response.raise_for_status()
    return parse_feed_xml(response.text, source)[:limit]


def parse_feed_xml(xml_text: str, source: SourceConfig) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    tag = _local_name(root.tag)
    if tag == "feed":
        return _parse_atom_feed(root, source)
    return _parse_rss_feed(root, source)


def _parse_rss_feed(root: ET.Element, source: SourceConfig) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _clean_text(_child_text(item, "title"))
        raw_url = _clean_text(_child_text(item, "link"))
        canonical_url = canonicalize_url(raw_url)
        external_id = _clean_text(_child_text(item, "guid")) or canonical_url or title
        excerpt = _clean_text(
            _child_text(item, "description")
            or _child_text(item, "summary")
            or _child_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
        )
        content_text = _clean_text(
            _child_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
            or excerpt
        )
        items.append(
            {
                "external_id": external_id,
                "title": title,
                "raw_url": raw_url,
                "canonical_url": canonical_url,
                "excerpt": excerpt[:1200],
                "content_text": content_text[:2400],
                "author": _clean_text(
                    _child_text(item, "author")
                    or _child_text(item, "{http://purl.org/dc/elements/1.1/}creator")
                ),
                "published_at": parse_datetime(
                    _child_text(item, "pubDate") or _child_text(item, "published")
                ),
                "categories": [
                    _clean_text(child.text or "")
                    for child in item.findall("category")
                    if _clean_text(child.text or "")
                ],
                "excerpt_only": not content_text or content_text[:400] == excerpt[:400],
                "raw_payload": {
                    "title": title,
                    "url": raw_url,
                    "categories": [
                        _clean_text(child.text or "")
                        for child in item.findall("category")
                        if _clean_text(child.text or "")
                    ],
                },
            }
        )
    return items


def _parse_atom_feed(root: ET.Element, source: SourceConfig) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    namespace = _namespace(root.tag)
    entry_tag = f"{{{namespace}}}entry" if namespace else "entry"
    for entry in root.findall(entry_tag):
        title = _clean_text(_child_text(entry, _qualified(namespace, "title")))
        link = ""
        for child in entry.findall(_qualified(namespace, "link")):
            rel = child.attrib.get("rel", "alternate")
            if rel == "alternate":
                link = child.attrib.get("href", "")
                break
        raw_url = _clean_text(link)
        canonical_url = canonicalize_url(raw_url)
        external_id = (
            _clean_text(_child_text(entry, _qualified(namespace, "id")))
            or canonical_url
            or title
        )
        excerpt = _clean_text(
            _child_text(entry, _qualified(namespace, "summary"))
            or _child_text(entry, _qualified(namespace, "content"))
        )
        content_text = _clean_text(
            _child_text(entry, _qualified(namespace, "content")) or excerpt
        )
        author = ""
        author_node = entry.find(_qualified(namespace, "author"))
        if author_node is not None:
            author = _clean_text(
                _child_text(author_node, _qualified(namespace, "name"))
            )
        items.append(
            {
                "external_id": external_id,
                "title": title,
                "raw_url": raw_url,
                "canonical_url": canonical_url,
                "excerpt": excerpt[:1200],
                "content_text": content_text[:2400],
                "author": author,
                "published_at": parse_datetime(
                    _child_text(entry, _qualified(namespace, "published"))
                    or _child_text(entry, _qualified(namespace, "updated"))
                ),
                "categories": [
                    _clean_text(node.attrib.get("term", ""))
                    for node in entry.findall(_qualified(namespace, "category"))
                    if _clean_text(node.attrib.get("term", ""))
                ],
                "excerpt_only": not content_text or content_text[:400] == excerpt[:400],
                "raw_payload": {"title": title, "url": raw_url},
            }
        )
    return items


async def store_articles(
    pool: asyncpg.Pool,
    source: SourceConfig,
    items: list[dict[str, Any]],
) -> list[str]:
    inserted: list[str] = []
    for item in items:
        article_key = hashlib.sha256(
            (
                f"{source.source_key}|{item.get('external_id') or ''}|"
                f"{item.get('canonical_url') or item.get('title') or ''}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        result = await pool.fetchval(
            "INSERT INTO policy_news_articles ("
            "article_key, source_key, external_id, title, title_normalized, canonical_url, raw_url, "
            "excerpt, content_text, author, published_at, categories, raw_payload, excerpt_only"
            ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13::jsonb, $14) "
            "ON CONFLICT (article_key) DO NOTHING RETURNING article_key",
            article_key,
            source.source_key,
            str(item.get("external_id") or ""),
            title,
            normalize_title(title),
            str(item.get("canonical_url") or ""),
            str(item.get("raw_url") or ""),
            str(item.get("excerpt") or ""),
            str(item.get("content_text") or ""),
            str(item.get("author") or ""),
            item.get("published_at"),
            json.dumps(item.get("categories") or []),
            json.dumps(item.get("raw_payload") or {}),
            bool(item.get("excerpt_only")),
        )
        if result:
            inserted.append(str(result))
    return inserted


async def list_unprocessed_articles(
    pool: asyncpg.Pool,
    *,
    limit: int,
) -> list[ArticleCandidate]:
    rows = await pool.fetch(
        "SELECT a.article_key, a.source_key, s.name AS source_name, s.source_kind, s.trust_tier, "
        "s.topic_hints, a.title, a.title_normalized, a.canonical_url, a.excerpt, a.content_text, "
        "a.author, a.published_at, a.categories, a.excerpt_only "
        "FROM policy_news_articles a "
        "JOIN policy_news_sources s ON s.source_key = a.source_key "
        "WHERE a.processed_at IS NULL "
        "ORDER BY COALESCE(a.published_at, a.ingested_at) DESC "
        "LIMIT $1",
        limit,
    )
    return [
        ArticleCandidate(
            article_key=str(row["article_key"]),
            source_key=str(row["source_key"]),
            source_name=str(row["source_name"]),
            source_kind=str(row["source_kind"]),
            trust_tier=int(row["trust_tier"] or 3),
            title=str(row["title"]),
            title_normalized=str(row["title_normalized"]),
            canonical_url=str(row["canonical_url"] or ""),
            excerpt=str(row["excerpt"] or ""),
            content_text=str(row["content_text"] or ""),
            author=str(row["author"] or ""),
            published_at=row["published_at"],
            categories=list(row["categories"] or []),
            excerpt_only=bool(row["excerpt_only"]),
            topic_hints=list(row["topic_hints"] or []),
        )
        for row in rows
    ]


async def classify_candidates(
    config: MonitorConfig,
    articles: list[ArticleCandidate],
) -> dict[str, ClassificationResult]:
    if not articles:
        return {}
    provider = choose_provider(config.classifier_provider)
    results: dict[str, ClassificationResult] = {}
    batch_size = 12
    for start in range(0, len(articles), batch_size):
        batch = articles[start : start + batch_size]
        if provider == "anthropic":
            batch_results = await classify_with_anthropic(config, batch)
        else:
            batch_results = {
                article.article_key: heuristic_classify(article) for article in batch
            }
        for article in batch:
            result = batch_results.get(article.article_key) or heuristic_classify(
                article
            )
            enriched = finalize_classification(article, result)
            results[article.article_key] = enriched
    return results


def choose_provider(requested: str) -> str:
    mode = requested.strip().lower()
    if mode in {"heuristic", "anthropic"}:
        return mode
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "heuristic"


async def classify_with_anthropic(
    config: MonitorConfig,
    batch: list[ArticleCandidate],
) -> dict[str, ClassificationResult]:
    prompt = build_classifier_prompt(batch)
    model = config.model or os.getenv("POLICY_NEWS_MODEL") or "claude-sonnet-4-20250514"
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

    def _call() -> str:
        message = client.messages.create(
            model=model,
            max_tokens=3500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = []
        for block in message.content:
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
        return "\n".join(parts)

    response_text = await asyncio.to_thread(_call)
    payload = extract_json_payload(response_text)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("classifier response missing items array")
    results: dict[str, ClassificationResult] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        article_key = str(item.get("article_key") or "").strip()
        if not article_key:
            continue
        scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
        results[article_key] = ClassificationResult(
            article_key=article_key,
            primary_topic=str(item.get("primary_topic") or "").strip(),
            secondary_tags=sorted(
                {
                    str(tag).strip()
                    for tag in item.get("secondary_tags") or []
                    if str(tag).strip()
                }
            ),
            include=bool(item.get("include")),
            reason_for_inclusion=str(item.get("reason_for_inclusion") or "").strip(),
            what_happened=str(item.get("what_happened") or "").strip(),
            why_it_matters=str(item.get("why_it_matters") or "").strip(),
            classifier_notes=str(item.get("classifier_notes") or "").strip(),
            suggested_delivery=str(
                item.get("delivery_class") or "Archive Only"
            ).strip(),
            scores={
                "policy_centrality": int(scores.get("policy_centrality") or 0),
                "actor_importance": int(scores.get("actor_importance") or 0),
                "actionability": int(scores.get("actionability") or 0),
                "source_quality": 0,
                "novelty": int(scores.get("novelty") or 0),
                "narrative_influence": int(scores.get("narrative_influence") or 0),
            },
            exclusion_reason=str(item.get("exclusion_reason") or "").strip(),
        )
    return results


def build_classifier_prompt(batch: list[ArticleCandidate]) -> str:
    serialized = []
    for article in batch:
        serialized.append(
            {
                "article_key": article.article_key,
                "source_name": article.source_name,
                "source_kind": article.source_kind,
                "trust_tier": article.trust_tier,
                "topic_hints": article.topic_hints,
                "title": article.title,
                "excerpt": article.excerpt,
                "content_text": article.content_text[:1000],
                "author": article.author,
                "published_at": article.published_at.isoformat()
                if article.published_at
                else "",
                "excerpt_only": article.excerpt_only,
                "watch_hits": watch_term_hits(article),
            }
        )
    return (
        "You are classifying curated RSS items for a high-signal policy news monitor.\n"
        "The monitor covers exactly these primary topics: Crypto, AI, Prediction Markets, Defense Tech.\n"
        "Secondary tags can include Congress, Agency, Opinion, Narrative, Enforcement, Markup, Live Update.\n"
        "The goal is to minimize misses while keeping obvious non-policy noise out.\n\n"
        "Important inclusion rules:\n"
        "- Include legislative, regulatory, agency, judicial, enforcement, procurement, export-control, "
        "committee, floor-action, or meaningful member/agency press-release developments.\n"
        "- Include some top-tier narrative/opinion coverage when it is written by a meaningful principal "
        "or clearly shapes elite discourse.\n"
        "- Reject keyword-only mentions with no central policy angle.\n"
        "- Usually reject startup launches, funding rounds, product refreshes, conferences, and generic "
        "market color unless policy/procurement/regulatory significance is central.\n"
        "- Defense-tech coverage should focus on procurement, congressional pressure, export controls, "
        "national security decisions, or real budget/process movement.\n"
        "- Prediction-market coverage should focus on CFTC action, litigation, congressional inquiry, "
        "agency guidance, or election-law implications, not product chatter.\n\n"
        "Score these fields except source_quality, which is handled separately:\n"
        "- policy_centrality: 0-30\n"
        "- actor_importance: 0-20\n"
        "- actionability: 0-15\n"
        "- novelty: 0-10\n"
        "- narrative_influence: 0-10\n\n"
        "Return only valid JSON matching this shape:\n"
        '{"items": ['
        '{"article_key": "...", "primary_topic": "Crypto|AI|Prediction Markets|Defense Tech|", '
        '"secondary_tags": ["..."], "include": true, "delivery_class": '
        '"Urgent|Standard|Narrative|Archive Only", "reason_for_inclusion": "...", '
        '"what_happened": "...", "why_it_matters": "...", "classifier_notes": "...", '
        '"scores": {"policy_centrality": 0, "actor_importance": 0, "actionability": 0, '
        '"novelty": 0, "narrative_influence": 0}, "exclusion_reason": "..."}]}\n\n'
        f"Articles:\n{json.dumps(serialized, indent=2)}"
    )


def heuristic_classify(article: ArticleCandidate) -> ClassificationResult:
    text = f" {article.title.lower()} {article.excerpt.lower()} {article.content_text.lower()} "
    topic = detect_primary_topic(text, article.topic_hints)
    policy_hits = count_hits(
        text, CONGRESS_TERMS + AGENCY_TERMS + ENFORCEMENT_TERMS + MARKUP_TERMS
    )
    topic_hits = count_hits(
        text, [kw for values in TOPIC_KEYWORDS.values() for kw in values]
    )
    noise_hits = count_hits(text, NOISE_TERMS)
    include = bool(
        topic and (policy_hits > 0 or topic_hits > 1 or article.source_kind != "news")
    )
    secondary_tags = infer_secondary_tags(article, narrative_score=2)
    delivery = "Archive Only"
    if include and policy_hits >= 2:
        delivery = "Standard"
    if include and any(term in text for term in URGENT_TERMS):
        delivery = "Urgent"
    if "Opinion" in secondary_tags or "Narrative" in secondary_tags:
        delivery = "Narrative" if include else "Archive Only"
    reason = (
        "heuristic policy match" if include else "low-confidence heuristic fallback"
    )
    scores = {
        "policy_centrality": min(
            30, policy_hits * 5 + max(topic_hits - noise_hits, 0) * 2
        ),
        "actor_importance": min(20, policy_hits * 3),
        "actionability": min(15, 10 if delivery == "Urgent" else policy_hits * 2),
        "source_quality": 0,
        "novelty": 5,
        "narrative_influence": 5 if "Narrative" in secondary_tags else 1,
    }
    return ClassificationResult(
        article_key=article.article_key,
        primary_topic=topic,
        secondary_tags=secondary_tags,
        include=include,
        reason_for_inclusion=reason,
        what_happened=article.title,
        why_it_matters=(
            "Potential policy signal from a curated source."
            if include
            else "This looks too weak to alert without stronger policy context."
        ),
        classifier_notes="heuristic fallback",
        suggested_delivery=delivery,
        scores=scores,
        exclusion_reason=""
        if include
        else "heuristic fallback rejected low-policy item",
    )


def finalize_classification(
    article: ArticleCandidate,
    result: ClassificationResult,
) -> ClassificationResult:
    source_points = source_quality_points(article.trust_tier)
    result.scores["source_quality"] = source_points
    result.secondary_tags = infer_secondary_tags(
        article,
        narrative_score=result.scores.get("narrative_influence", 0),
        base_tags=result.secondary_tags,
    )
    apply_priority_boosts(article, result)
    if not result.primary_topic:
        result.primary_topic = detect_primary_topic(
            article_text(article), article.topic_hints
        )
    result.include = result.include and bool(result.primary_topic)
    result.suggested_delivery = route_delivery_class(article, result)
    if result.suggested_delivery == "Archive Only":
        result.include = False
    if not result.reason_for_inclusion:
        result.reason_for_inclusion = default_reason_for_inclusion(result)
    if not result.what_happened:
        result.what_happened = article.title
    if not result.why_it_matters:
        result.why_it_matters = default_why_it_matters(result)
    return result


def source_quality_points(trust_tier: int) -> int:
    return {1: 6, 2: 8, 3: 10, 4: 12, 5: 15}.get(max(1, min(trust_tier, 5)), 10)


def article_text(article: ArticleCandidate) -> str:
    return f" {article.title.lower()} {article.excerpt.lower()} {article.content_text.lower()} "


def watch_term_hits(article: ArticleCandidate) -> list[str]:
    text = article_text(article)
    hits = [term for term, _, _ in WATCH_TERMS if term.lower() in text]
    for agency in PRIORITY_AGENCIES:
        if agency.lower() in text and agency not in hits:
            hits.append(agency)
    return hits[:12]


def apply_priority_boosts(
    article: ArticleCandidate, result: ClassificationResult
) -> None:
    text = article_text(article)
    watch_hits = watch_term_hits(article)
    policy_boost = sum(boost for term, _, boost in WATCH_TERMS if term in watch_hits)
    result.scores["policy_centrality"] = min(
        30,
        int(result.scores.get("policy_centrality", 0)) + min(policy_boost, 6),
    )
    actor_boost = 0
    if any(term.lower() in text for term in PRIORITY_AGENCIES):
        actor_boost += 4
    if article.source_kind in {"agency_release", "member_press_release"}:
        actor_boost += 4
    if any(
        term in text
        for term in ["chairman", "chairwoman", "ranking member", "committee"]
    ):
        actor_boost += 3
    result.scores["actor_importance"] = min(
        20,
        int(result.scores.get("actor_importance", 0)) + actor_boost,
    )
    actionability_boost = 0
    if any(term in text for term in MARKUP_TERMS):
        actionability_boost += 5
    if any(term in text for term in ENFORCEMENT_TERMS):
        actionability_boost += 4
    if any(term in text for term in LIVE_UPDATE_TERMS):
        actionability_boost += 1
    result.scores["actionability"] = min(
        15,
        int(result.scores.get("actionability", 0)) + actionability_boost,
    )


def route_delivery_class(
    article: ArticleCandidate, result: ClassificationResult
) -> str:
    if not result.include:
        return "Archive Only"
    total = result.total_score
    text = article_text(article)
    if any(term in text for term in URGENT_TERMS):
        return "Urgent"
    if total >= 85:
        return "Urgent"
    if total >= 65:
        return "Standard"
    if total >= 45 and (
        "Opinion" in result.secondary_tags
        or "Narrative" in result.secondary_tags
        or int(result.scores.get("narrative_influence", 0)) >= 7
        or result.suggested_delivery == "Narrative"
    ):
        return "Narrative"
    if total >= 55 and (
        "Congress" in result.secondary_tags
        or "Agency" in result.secondary_tags
        or "Markup" in result.secondary_tags
        or "Enforcement" in result.secondary_tags
    ):
        return "Standard"
    return "Archive Only"


def infer_secondary_tags(
    article: ArticleCandidate,
    narrative_score: int,
    base_tags: list[str] | None = None,
) -> list[str]:
    tags = set(base_tags or [])
    text = article_text(article)
    if any(term in text for term in CONGRESS_TERMS):
        tags.add("Congress")
    if (
        any(term in text for term in AGENCY_TERMS)
        or article.source_kind == "agency_release"
    ):
        tags.add("Agency")
    if any(term in text for term in ENFORCEMENT_TERMS):
        tags.add("Enforcement")
    if any(term in text for term in MARKUP_TERMS):
        tags.add("Markup")
    if any(term in text for term in LIVE_UPDATE_TERMS):
        tags.add("Live Update")
    if (
        any(term in text for term in OPINION_TERMS)
        or "/opinion/" in article.canonical_url
    ):
        tags.add("Opinion")
    if "Opinion" in tags or narrative_score >= 7:
        tags.add("Narrative")
    return sorted(tags)


def detect_primary_topic(text: str, hints: list[str]) -> str:
    lowered = text.lower()
    scores: dict[str, int] = {topic: 0 for topic in TOPIC_ORDER}
    for hint in hints:
        if hint in scores:
            scores[hint] += 2
    for topic, keywords in TOPIC_KEYWORDS.items():
        for keyword in keywords:
            needle = keyword.lower()
            if needle == " ai ":
                if re.search(r"\bai\b", lowered):
                    scores[topic] += 1
                continue
            if needle in lowered:
                scores[topic] += 1
    best = max(scores.items(), key=lambda item: item[1])
    return best[0] if best[1] > 0 else ""


def default_reason_for_inclusion(result: ClassificationResult) -> str:
    if "Markup" in result.secondary_tags:
        return "committee activity"
    if "Enforcement" in result.secondary_tags:
        return "court or enforcement action"
    if "Agency" in result.secondary_tags:
        return "agency release"
    if "Narrative" in result.secondary_tags:
        return "major outlet narrative"
    if "Congress" in result.secondary_tags:
        return "congressional activity"
    return "policy-relevant development"


def default_why_it_matters(result: ClassificationResult) -> str:
    if "Narrative" in result.secondary_tags:
        return "Useful as a narrative signal even if it is not a new policy event."
    if result.suggested_delivery == "Urgent":
        return "This looks like a concrete process or enforcement development worth seeing immediately."
    return "This appears material enough to track in the policy feed and archive."


async def upsert_clusters(
    pool: asyncpg.Pool,
    articles: list[ArticleCandidate],
    results: dict[str, ClassificationResult],
) -> list[dict[str, Any]]:
    recent_clusters = await load_recent_clusters(pool)
    cluster_lookup = {cluster["cluster_id"]: cluster for cluster in recent_clusters}
    ready_for_alert: dict[str, dict[str, Any]] = {}
    for article in articles:
        result = results[article.article_key]
        cluster = find_matching_cluster(article, recent_clusters)
        if cluster is None:
            cluster_id = f"clu_{uuid.uuid4().hex[:12]}"
            cluster = {
                "cluster_id": cluster_id,
                "canonical_title": article.title,
                "title_normalized": article.title_normalized,
                "title_tokens": sorted(tokenize_title(article.title)),
                "canonical_url": article.canonical_url,
                "primary_topic": result.primary_topic,
                "secondary_tags": list(result.secondary_tags),
                "score_total": result.total_score,
                "score_breakdown": dict(result.scores),
                "delivery_class": result.suggested_delivery,
                "reason_for_inclusion": result.reason_for_inclusion,
                "what_happened": result.what_happened,
                "why_it_matters": result.why_it_matters,
                "classifier_notes": result.classifier_notes,
                "first_seen_at": article.published_at
                or dt.datetime.now(dt.timezone.utc),
                "last_alerted_at": None,
                "existing_delivery_class": "Archive Only",
            }
            recent_clusters.append(cluster)
            cluster_lookup[cluster_id] = cluster
        else:
            if result.total_score >= int(cluster.get("score_total") or 0):
                cluster["canonical_title"] = article.title
                cluster["canonical_url"] = (
                    article.canonical_url or cluster.get("canonical_url") or ""
                )
                cluster["primary_topic"] = result.primary_topic
                cluster["secondary_tags"] = sorted(
                    set(cluster.get("secondary_tags") or []).union(
                        result.secondary_tags
                    )
                )
                cluster["score_total"] = result.total_score
                cluster["score_breakdown"] = dict(result.scores)
                cluster["delivery_class"] = result.suggested_delivery
                cluster["reason_for_inclusion"] = result.reason_for_inclusion
                cluster["what_happened"] = result.what_happened
                cluster["why_it_matters"] = result.why_it_matters
                cluster["classifier_notes"] = result.classifier_notes
            cluster["first_seen_at"] = min(
                cluster.get("first_seen_at") or dt.datetime.now(dt.timezone.utc),
                article.published_at or dt.datetime.now(dt.timezone.utc),
            )
        await persist_cluster(pool, cluster)
        await pool.execute(
            "UPDATE policy_news_articles SET processed_at = NOW(), cluster_id = $2, classification_json = $3::jsonb "
            "WHERE article_key = $1",
            article.article_key,
            str(cluster["cluster_id"]),
            json.dumps(asdict(result)),
        )
        similarity = title_similarity(
            article.title_normalized, str(cluster["title_normalized"])
        )
        await pool.execute(
            "INSERT INTO policy_news_cluster_articles (cluster_id, article_key, is_primary, similarity) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (cluster_id, article_key) DO UPDATE SET "
            "is_primary = EXCLUDED.is_primary, similarity = EXCLUDED.similarity",
            str(cluster["cluster_id"]),
            article.article_key,
            article.title == cluster.get("canonical_title"),
            similarity,
        )
        existing_delivery = str(
            cluster.get("existing_delivery_class") or "Archive Only"
        )
        last_alerted_at = cluster.get("last_alerted_at")
        if should_alert(
            existing_delivery, str(cluster["delivery_class"]), last_alerted_at
        ):
            ready_for_alert[str(cluster["cluster_id"])] = dict(cluster)
            cluster["existing_delivery_class"] = str(cluster["delivery_class"])
    return list(ready_for_alert.values())


async def load_recent_clusters(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT cluster_id, canonical_title, title_normalized, title_tokens, canonical_url, primary_topic, "
        "secondary_tags, score_total, score_breakdown, delivery_class, reason_for_inclusion, "
        "what_happened, why_it_matters, classifier_notes, first_seen_at, last_alerted_at "
        "FROM policy_news_clusters WHERE first_seen_at >= NOW() - INTERVAL '7 days'"
    )
    clusters = []
    for row in rows:
        clusters.append(
            {
                "cluster_id": str(row["cluster_id"]),
                "canonical_title": str(row["canonical_title"]),
                "title_normalized": str(row["title_normalized"]),
                "title_tokens": list(row["title_tokens"] or []),
                "canonical_url": str(row["canonical_url"] or ""),
                "primary_topic": str(row["primary_topic"] or ""),
                "secondary_tags": list(row["secondary_tags"] or []),
                "score_total": int(row["score_total"] or 0),
                "score_breakdown": dict(row["score_breakdown"] or {}),
                "delivery_class": str(row["delivery_class"] or "Archive Only"),
                "existing_delivery_class": str(row["delivery_class"] or "Archive Only"),
                "reason_for_inclusion": str(row["reason_for_inclusion"] or ""),
                "what_happened": str(row["what_happened"] or ""),
                "why_it_matters": str(row["why_it_matters"] or ""),
                "classifier_notes": str(row["classifier_notes"] or ""),
                "first_seen_at": row["first_seen_at"],
                "last_alerted_at": row["last_alerted_at"],
            }
        )
    return clusters


def find_matching_cluster(
    article: ArticleCandidate,
    recent_clusters: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for cluster in recent_clusters:
        if article.title_normalized == cluster.get("title_normalized"):
            return cluster
        score = title_similarity(
            article.title_normalized, str(cluster.get("title_normalized") or "")
        )
        if score >= 0.70 and score > best_score:
            best = cluster
            best_score = score
    return best


async def persist_cluster(pool: asyncpg.Pool, cluster: dict[str, Any]) -> None:
    await pool.execute(
        "INSERT INTO policy_news_clusters ("
        "cluster_id, canonical_title, title_normalized, title_tokens, canonical_url, primary_topic, "
        "secondary_tags, score_total, score_breakdown, delivery_class, reason_for_inclusion, "
        "what_happened, why_it_matters, classifier_notes, first_seen_at, updated_at, last_alerted_at"
        ") VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9::jsonb, $10, $11, $12, $13, $14, $15, NOW(), $16) "
        "ON CONFLICT (cluster_id) DO UPDATE SET canonical_title = EXCLUDED.canonical_title, "
        "title_normalized = EXCLUDED.title_normalized, title_tokens = EXCLUDED.title_tokens, "
        "canonical_url = EXCLUDED.canonical_url, primary_topic = EXCLUDED.primary_topic, "
        "secondary_tags = EXCLUDED.secondary_tags, score_total = EXCLUDED.score_total, "
        "score_breakdown = EXCLUDED.score_breakdown, delivery_class = EXCLUDED.delivery_class, "
        "reason_for_inclusion = EXCLUDED.reason_for_inclusion, what_happened = EXCLUDED.what_happened, "
        "why_it_matters = EXCLUDED.why_it_matters, classifier_notes = EXCLUDED.classifier_notes, "
        "first_seen_at = LEAST(policy_news_clusters.first_seen_at, EXCLUDED.first_seen_at), "
        "updated_at = NOW(), last_alerted_at = COALESCE(policy_news_clusters.last_alerted_at, EXCLUDED.last_alerted_at)",
        str(cluster["cluster_id"]),
        str(cluster["canonical_title"]),
        str(cluster["title_normalized"]),
        json.dumps(cluster.get("title_tokens") or []),
        str(cluster.get("canonical_url") or ""),
        str(cluster.get("primary_topic") or ""),
        json.dumps(cluster.get("secondary_tags") or []),
        int(cluster.get("score_total") or 0),
        json.dumps(cluster.get("score_breakdown") or {}),
        str(cluster.get("delivery_class") or "Archive Only"),
        str(cluster.get("reason_for_inclusion") or ""),
        str(cluster.get("what_happened") or ""),
        str(cluster.get("why_it_matters") or ""),
        str(cluster.get("classifier_notes") or ""),
        cluster.get("first_seen_at") or dt.datetime.now(dt.timezone.utc),
        cluster.get("last_alerted_at"),
    )


def should_alert(
    existing_delivery: str,
    new_delivery: str,
    last_alerted_at: dt.datetime | None,
) -> bool:
    if new_delivery == "Archive Only":
        return False
    if last_alerted_at is None:
        return True
    if existing_delivery != "Urgent" and new_delivery == "Urgent":
        return True
    return False


async def build_alert_message(pool: asyncpg.Pool, cluster: dict[str, Any]) -> str:
    rows = await pool.fetch(
        "SELECT s.name FROM policy_news_cluster_articles ca "
        "JOIN policy_news_articles a ON a.article_key = ca.article_key "
        "JOIN policy_news_sources s ON s.source_key = a.source_key "
        "WHERE ca.cluster_id = $1 ORDER BY ca.is_primary DESC, s.trust_tier DESC, a.published_at DESC NULLS LAST",
        str(cluster["cluster_id"]),
    )
    sources = []
    for row in rows:
        name = str(row["name"] or "")
        if name and name not in sources:
            sources.append(name)
    tag_line = [str(cluster.get("primary_topic") or "")]
    secondary_tags = [
        tag for tag in cluster.get("secondary_tags") or [] if tag != "Narrative"
    ]
    tag_line.extend(secondary_tags[:2])
    tag_line.append(str(cluster.get("delivery_class") or "Standard"))
    header = "".join(f"[{tag}]" for tag in tag_line if tag)
    corroborating = ", ".join(sources[1:4])
    lines = [
        header,
        str(cluster.get("canonical_title") or ""),
        f"Source: {sources[0] if sources else 'Unknown'}",
        str(cluster.get("what_happened") or ""),
        f"Why it matters: {cluster.get('why_it_matters') or ''}",
        f"Reason for inclusion: {cluster.get('reason_for_inclusion') or ''}",
    ]
    if corroborating:
        lines.append(f"Corroborating coverage: {corroborating}")
    url = str(cluster.get("canonical_url") or "")
    if url:
        lines.append(url)
    return "\n".join(line for line in lines if line)


async def record_alert(
    pool: asyncpg.Pool,
    *,
    cluster_id: str,
    channel_id: str,
    thread_ts: str,
    delivery_class: str,
    message_text: str,
    reason_for_inclusion: str,
    score_total: int,
) -> None:
    alert_id = f"alt_{uuid.uuid4().hex[:12]}"
    await pool.execute(
        "INSERT INTO policy_news_alerts ("
        "alert_id, cluster_id, slack_channel_id, slack_thread_ts, delivery_class, message_text, "
        "reason_for_inclusion, score_total, last_scanned_reply_ts"
        ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $4)",
        alert_id,
        cluster_id,
        channel_id,
        thread_ts,
        delivery_class,
        message_text,
        reason_for_inclusion,
        score_total,
    )


async def process_alert_replies(
    ctx: WorkflowContext,
    config: MonitorConfig,
) -> tuple[int, int]:
    rows = await ctx._pool.fetch(
        "SELECT alert_id, slack_channel_id, slack_thread_ts, last_scanned_reply_ts "
        "FROM policy_news_alerts WHERE sent_at >= NOW() - INTERVAL '21 days' "
        "ORDER BY sent_at DESC LIMIT 100"
    )
    feedback_count = 0
    query_reply_count = 0
    source_names = [source.name for source in config.sources]
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        channel_id = str(row["slack_channel_id"])
        thread_ts = str(row["slack_thread_ts"])
        last_seen = str(row["last_scanned_reply_ts"] or thread_ts)
        replies = await ctx.call_tool(
            "slack",
            "get_thread_replies",
            {"channel_id": channel_id, "thread_ts": thread_ts, "limit": 50},
        )
        newest_seen = last_seen
        for reply in replies or []:
            reply_ts = str(reply.get("timestamp") or "")
            if not reply_ts or reply_ts == thread_ts:
                continue
            if slack_ts_value(reply_ts) <= slack_ts_value(last_seen):
                continue
            text = normalize_reply_text(str(reply.get("text") or ""))
            if not text:
                newest_seen = max_slack_ts(newest_seen, reply_ts)
                continue
            feedback = parse_feedback_command(text)
            if feedback:
                await ctx._pool.execute(
                    "INSERT INTO policy_news_feedback ("
                    "alert_id, slack_channel_id, slack_thread_ts, reply_ts, reply_user, reply_text, command, note"
                    ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (alert_id, reply_ts) DO NOTHING",
                    str(row["alert_id"]),
                    channel_id,
                    thread_ts,
                    reply_ts,
                    str(reply.get("user") or ""),
                    str(reply.get("text") or ""),
                    feedback.command,
                    feedback.note,
                )
                feedback_count += 1
            else:
                query = parse_query_request(text, now=now, source_names=source_names)
                if query:
                    response = await search_archive(
                        ctx._pool, query, limit=config.max_query_results
                    )
                    await ctx.post_to_slack(channel_id, response, thread_ts=thread_ts)
                    query_reply_count += 1
            newest_seen = max_slack_ts(newest_seen, reply_ts)
        if newest_seen != last_seen:
            await ctx._pool.execute(
                "UPDATE policy_news_alerts SET last_scanned_reply_ts = $2 WHERE alert_id = $1",
                str(row["alert_id"]),
                newest_seen,
            )
    return feedback_count, query_reply_count


def parse_feedback_command(text: str) -> FeedbackCommand | None:
    lowered = normalize_reply_text(text).lower()
    for prefix in FEEDBACK_PREFIXES:
        if lowered == prefix:
            return FeedbackCommand(command=prefix)
        if lowered.startswith(prefix + ":"):
            note = lowered.split(":", 1)[1].strip()
            return FeedbackCommand(command=prefix, note=note)
        if lowered.startswith(prefix + " "):
            note = lowered[len(prefix) :].strip()
            return FeedbackCommand(command=prefix, note=note)
    return None


def parse_query_request(
    text: str,
    *,
    now: dt.datetime,
    source_names: list[str],
) -> QueryRequest | None:
    normalized = normalize_reply_text(text)
    lowered = normalized.lower()
    if not lowered.startswith(QUERY_PREFIXES):
        return None
    query = QueryRequest(
        raw_text=normalized, sent_only=lowered.startswith("what did we send")
    )
    working = lowered
    if match := re.search(r"(?:last|past)\s+(\d+)\s*d(?:ays?)?", working):
        query.since = now - dt.timedelta(days=int(match.group(1)))
        working = working.replace(match.group(0), " ")
    elif match := re.search(r"(?:last|past)\s+(\d+)\s*w(?:eeks?)?", working):
        query.since = now - dt.timedelta(weeks=int(match.group(1)))
        working = working.replace(match.group(0), " ")
    elif "this month" in working:
        query.since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        working = working.replace("this month", " ")
    elif "this week" in working:
        query.since = (now - dt.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        working = working.replace("this week", " ")
    elif "today" in working:
        query.since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        working = working.replace("today", " ")
    for topic in TOPIC_ORDER:
        if topic.lower() in working:
            query.topic = topic
            working = working.replace(topic.lower(), " ")
            break
    for delivery in ["Urgent", "Standard", "Narrative"]:
        if delivery.lower() in working:
            query.delivery_class = delivery
            working = working.replace(delivery.lower(), " ")
            break
    matched_sources = []
    for source_name in sorted(source_names, key=len, reverse=True):
        source_lower = source_name.lower()
        if source_lower in working:
            matched_sources.append(source_name)
            working = working.replace(source_lower, " ")
    query.source_names = matched_sources
    filler = [
        "search",
        "show",
        "find",
        "all",
        "items",
        "mentioning",
        "mentions",
        "what",
        "did",
        "we",
        "send",
        "on",
    ]
    for token in filler:
        working = re.sub(rf"\b{re.escape(token)}\b", " ", working)
    query.search_text = re.sub(r"\s+", " ", working).strip()
    return query


async def search_archive(
    pool: asyncpg.Pool,
    query: QueryRequest,
    *,
    limit: int,
) -> str:
    clauses = []
    params: list[Any] = []
    joins = [
        "LEFT JOIN LATERAL ("
        "  SELECT a.sent_at, a.delivery_class AS sent_delivery_class "
        "  FROM policy_news_alerts a WHERE a.cluster_id = c.cluster_id "
        "  ORDER BY a.sent_at DESC LIMIT 1"
        ") latest_alert ON TRUE",
        "LEFT JOIN LATERAL ("
        "  SELECT string_agg(DISTINCT s.name, ', ' ORDER BY s.name) AS source_names "
        "  FROM policy_news_cluster_articles ca "
        "  JOIN policy_news_articles a ON a.article_key = ca.article_key "
        "  JOIN policy_news_sources s ON s.source_key = a.source_key "
        "  WHERE ca.cluster_id = c.cluster_id"
        ") cluster_sources ON TRUE",
    ]
    if query.sent_only:
        clauses.append("latest_alert.sent_at IS NOT NULL")
    if query.topic:
        params.append(query.topic)
        clauses.append(f"c.primary_topic = ${len(params)}")
    if query.delivery_class:
        params.append(query.delivery_class)
        clauses.append(
            f"COALESCE(latest_alert.sent_delivery_class, c.delivery_class) = ${len(params)}"
        )
    if query.since is not None:
        params.append(query.since)
        clauses.append(
            f"COALESCE(latest_alert.sent_at, c.first_seen_at) >= ${len(params)}"
        )
    if query.source_names:
        params.append(query.source_names)
        clauses.append(
            f"EXISTS (SELECT 1 FROM policy_news_cluster_articles ca2 "
            f"JOIN policy_news_articles a2 ON a2.article_key = ca2.article_key "
            f"JOIN policy_news_sources s2 ON s2.source_key = a2.source_key "
            f"WHERE ca2.cluster_id = c.cluster_id AND s2.name = ANY(${len(params)}::text[]))"
        )
    if query.search_text:
        params.append(query.search_text)
        clauses.append(
            f"to_tsvector('english', coalesce(c.canonical_title, '') || ' ' || "
            f"coalesce(c.what_happened, '') || ' ' || coalesce(c.why_it_matters, '') || ' ' || "
            f"coalesce(c.reason_for_inclusion, '')) @@ plainto_tsquery('english', ${len(params)})"
        )
    sql = (
        "SELECT c.cluster_id, c.canonical_title, c.primary_topic, c.secondary_tags, c.delivery_class, "
        "c.what_happened, c.why_it_matters, c.reason_for_inclusion, c.canonical_url, c.first_seen_at, "
        "latest_alert.sent_at, COALESCE(cluster_sources.source_names, '') AS source_names, "
        "COALESCE(latest_alert.sent_delivery_class, c.delivery_class) AS effective_delivery_class "
        "FROM policy_news_clusters c " + " ".join(joins)
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(latest_alert.sent_at, c.first_seen_at) DESC, c.score_total DESC LIMIT "
    params.append(limit)
    sql += f"${len(params)}"
    rows = await pool.fetch(sql, *params)
    if not rows:
        scope = "sent alerts" if query.sent_only else "archived items"
        return f"No {scope} matched `{query.raw_text}`."
    lines = []
    scope = "sent alerts" if query.sent_only else "archived items"
    lines.append(f"Found {len(rows)} {scope} for `{query.raw_text}`:")
    for row in rows:
        when = row["sent_at"] or row["first_seen_at"]
        date_str = (
            when.strftime("%Y-%m-%d")
            if isinstance(when, dt.datetime)
            else "unknown-date"
        )
        tags = [str(row["primary_topic"] or "")]
        tags.extend(list(row["secondary_tags"] or [])[:1])
        tags.append(str(row["effective_delivery_class"] or row["delivery_class"] or ""))
        tag_text = "".join(f"[{tag}]" for tag in tags if tag)
        line = (
            f"- {date_str} {tag_text} {row['canonical_title']}"
            f" - {row['source_names'] or 'Unknown source'}"
        )
        lines.append(line)
        why = str(row["why_it_matters"] or "").strip()
        if why:
            lines.append(f"  Why it mattered: {why}")
        url = str(row["canonical_url"] or "").strip()
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def normalize_reply_text(text: str) -> str:
    cleaned = re.sub(r"<@U[A-Z0-9]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_title(title: str) -> str:
    words = [token for token in tokenize_title(title) if token not in STOPWORDS]
    return " ".join(sorted(words))


def tokenize_title(title: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", title.lower()) if len(token) > 2
    }


def title_similarity(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return overlap / union if union else 0.0


def count_hits(text: str, keywords: list[str]) -> int:
    hits = 0
    for keyword in keywords:
        needle = keyword.lower()
        if needle == " ai ":
            if re.search(r"\bai\b", text):
                hits += 1
            continue
        if needle in text:
            hits += 1
    return hits


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    filtered = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"ref", "output", "fbclid"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), ""))


def parse_datetime(value: str) -> dt.datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        normalized = raw.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def extract_json_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped)
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        payload = json.loads(stripped)
        return payload if isinstance(payload, dict) else {"items": payload}
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", stripped, re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(1))
        return payload if isinstance(payload, dict) else {"items": payload}


def _clean_text(value: str) -> str:
    unescaped = html.unescape(value or "")
    no_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", no_tags).strip()


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _namespace(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def _qualified(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}" if namespace else local


def _child_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    if child is None or child.text is None:
        return ""
    return child.text


def slack_ts_value(value: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return Decimal(0)


def max_slack_ts(left: str, right: str) -> str:
    return right if slack_ts_value(right) > slack_ts_value(left) else left
