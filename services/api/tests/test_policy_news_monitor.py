from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
import pytest_asyncio

from api.policy_news import (
    DEFAULT_POLICY_NEWS_FEEDS_FILE,
    QueryRequest,
    build_alert_message,
    load_monitor_config,
    normalize_title,
    parse_feedback_command,
    parse_query_request,
    search_archive,
    title_similarity,
)


@pytest_asyncio.fixture
async def policy_news_tables(db_pool):
    await db_pool.execute(
        "TRUNCATE TABLE policy_news_feedback, policy_news_alerts, policy_news_cluster_articles, "
        "policy_news_articles, policy_news_clusters, policy_news_feed_fetches, "
        "policy_news_watch_terms, policy_news_sources CASCADE"
    )
    yield


def test_parse_feedback_command_supports_plain_and_annotated_commands():
    assert parse_feedback_command("good catch") == parse_feedback_command("good catch")
    detailed = parse_feedback_command("wrong topic: AI")
    assert detailed is not None
    assert detailed.command == "wrong topic"
    assert detailed.note == "ai"


def test_parse_query_request_extracts_topic_source_and_date_window():
    now = dt.datetime(2026, 4, 15, 12, 0, tzinfo=dt.timezone.utc)
    query = parse_query_request(
        "search crypto SEC last 30d Reuters",
        now=now,
        source_names=["Reuters", "Politico"],
    )
    assert query is not None
    assert query.topic == "Crypto"
    assert query.source_names == ["Reuters"]
    assert query.since == now - dt.timedelta(days=30)
    assert query.search_text == "sec"


def test_title_similarity_clusters_obvious_near_duplicates():
    left = normalize_title(
        "Chairman Scott announces digital asset market structure markup"
    )
    right = normalize_title(
        "Scott announces digital asset market structure markup in Senate Banking"
    )
    assert title_similarity(left, right) >= 0.70


def test_load_monitor_config_ignores_scheduler_metadata_and_uses_default_file(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "policy_news_sources.json"
    config_path.write_text(
        json.dumps(
            {
                "slack_channel": "C0ASR4NFLPR",
                "sources": [
                    {
                        "name": "Reuters",
                        "url": "https://www.reutersagency.com/feed/",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("POLICY_NEWS_FEEDS_FILE", str(config_path))

    config = load_monitor_config(
        {
            "metadata": {"source": "workflow_schedule"},
            "unexpected": "ignored",
        }
    )

    assert config.slack_channel == "C0ASR4NFLPR"
    assert config.sources[0].name == "Reuters"


def test_load_monitor_config_falls_back_to_checked_in_default_file(monkeypatch):
    monkeypatch.delenv("POLICY_NEWS_FEEDS_FILE", raising=False)

    assert DEFAULT_POLICY_NEWS_FEEDS_FILE == "/app/workflows/policy_news_sources.json"


@pytest.mark.asyncio
async def test_search_archive_filters_to_sent_alerts(db_pool, policy_news_tables):
    cluster_id = f"clu_{uuid.uuid4().hex[:12]}"
    article_key = uuid.uuid4().hex[:24]
    now = dt.datetime(2026, 4, 15, 12, 0, tzinfo=dt.timezone.utc)
    await db_pool.execute(
        "INSERT INTO policy_news_sources (source_key, name, feed_url) VALUES "
        "('reuters', 'Reuters', 'https://example.com/rss')"
    )
    await db_pool.execute(
        "INSERT INTO policy_news_clusters ("
        "cluster_id, canonical_title, title_normalized, title_tokens, canonical_url, primary_topic, "
        "secondary_tags, score_total, score_breakdown, delivery_class, reason_for_inclusion, "
        "what_happened, why_it_matters, first_seen_at"
        ") VALUES ($1, $2, $3, $4::jsonb, $5, 'Crypto', '[\"Congress\"]'::jsonb, 90, '{}'::jsonb, "
        "'Urgent', 'committee activity', $6, $7, $8)",
        cluster_id,
        "Chairman Scott announces digital asset market structure markup",
        normalize_title(
            "Chairman Scott announces digital asset market structure markup"
        ),
        '["chairman","scott","announces","digital","asset","market","structure","markup"]',
        "https://www.banking.senate.gov/newsroom/majority/chairman-scott-announces-digital-asset-market-structure-markup",
        "Senate Banking is moving market structure into formal committee process.",
        "This is a concrete process signal worth seeing immediately.",
        now,
    )
    await db_pool.execute(
        "INSERT INTO policy_news_articles ("
        "article_key, source_key, external_id, title, title_normalized, canonical_url, raw_url"
        ") VALUES ($1, 'reuters', 'ext-1', $2, $3, 'https://example.com/story', 'https://example.com/story')",
        article_key,
        "Chairman Scott announces digital asset market structure markup",
        normalize_title(
            "Chairman Scott announces digital asset market structure markup"
        ),
    )
    await db_pool.execute(
        "INSERT INTO policy_news_cluster_articles (cluster_id, article_key, is_primary) VALUES ($1, $2, TRUE)",
        cluster_id,
        article_key,
    )
    await db_pool.execute(
        "INSERT INTO policy_news_alerts ("
        "alert_id, cluster_id, slack_channel_id, slack_thread_ts, delivery_class, message_text, score_total"
        ") VALUES ('alt_1', $1, 'C123', '1776.0001', 'Urgent', 'posted', 90)",
        cluster_id,
    )

    result = await search_archive(
        db_pool,
        QueryRequest(
            raw_text="what did we send on crypto this month",
            sent_only=True,
            topic="Crypto",
            since=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        ),
        limit=5,
    )

    assert "Found 1 sent alerts" in result
    assert "Chairman Scott announces digital asset market structure markup" in result
    assert "[Crypto][Congress][Urgent]" in result


@pytest.mark.asyncio
async def test_build_alert_message_includes_reason_and_corroboration(
    db_pool, policy_news_tables
):
    cluster_id = f"clu_{uuid.uuid4().hex[:12]}"
    article_a = uuid.uuid4().hex[:24]
    article_b = uuid.uuid4().hex[:24]
    await db_pool.execute(
        "INSERT INTO policy_news_sources (source_key, name, feed_url, trust_tier) VALUES "
        "('reuters', 'Reuters', 'https://example.com/rss', 5), "
        "('politico', 'Politico', 'https://example.com/rss', 5)"
    )
    await db_pool.execute(
        "INSERT INTO policy_news_clusters ("
        "cluster_id, canonical_title, title_normalized, title_tokens, canonical_url, primary_topic, "
        "secondary_tags, score_total, score_breakdown, delivery_class, reason_for_inclusion, "
        "what_happened, why_it_matters"
        ") VALUES ($1, 'Test title', 'test title', '[\"test\",\"title\"]'::jsonb, 'https://example.com/story', "
        "'AI', '[\"Congress\"]'::jsonb, 70, '{}'::jsonb, 'Standard', 'committee activity', "
        "'What happened', 'Why it matters')",
        cluster_id,
    )
    await db_pool.execute(
        "INSERT INTO policy_news_articles (article_key, source_key, external_id, title, title_normalized) VALUES "
        "($1, 'reuters', 'a', 'A', 'a'), ($2, 'politico', 'b', 'B', 'b')",
        article_a,
        article_b,
    )
    await db_pool.execute(
        "INSERT INTO policy_news_cluster_articles (cluster_id, article_key, is_primary) VALUES "
        "($1, $2, TRUE), ($1, $3, FALSE)",
        cluster_id,
        article_a,
        article_b,
    )

    text = await build_alert_message(
        db_pool,
        {
            "cluster_id": cluster_id,
            "primary_topic": "AI",
            "secondary_tags": ["Congress"],
            "delivery_class": "Standard",
            "canonical_title": "Test title",
            "what_happened": "What happened",
            "why_it_matters": "Why it matters",
            "reason_for_inclusion": "committee activity",
            "canonical_url": "https://example.com/story",
        },
    )

    assert text.startswith("[AI][Congress][Standard]")
    assert "Reason for inclusion: committee activity" in text
    assert "Corroborating coverage: Politico" in text
