-- migrate:up

CREATE TABLE IF NOT EXISTS policy_news_sources (
    source_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'news',
    trust_tier SMALLINT NOT NULL DEFAULT 3,
    topic_hints JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS policy_news_watch_terms (
    term TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    boost SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS policy_news_feed_fetches (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES policy_news_sources(source_key) ON DELETE CASCADE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error_text TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_policy_news_feed_fetches_source_time
    ON policy_news_feed_fetches (source_key, fetched_at DESC);

CREATE TABLE IF NOT EXISTS policy_news_clusters (
    cluster_id TEXT PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    title_tokens JSONB NOT NULL DEFAULT '[]'::jsonb,
    canonical_url TEXT NOT NULL DEFAULT '',
    primary_topic TEXT NOT NULL DEFAULT '',
    secondary_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    score_total INTEGER NOT NULL DEFAULT 0,
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivery_class TEXT NOT NULL DEFAULT 'Archive Only',
    reason_for_inclusion TEXT NOT NULL DEFAULT '',
    what_happened TEXT NOT NULL DEFAULT '',
    why_it_matters TEXT NOT NULL DEFAULT '',
    classifier_notes TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_alerted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policy_news_clusters_first_seen
    ON policy_news_clusters (first_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_news_clusters_topic_delivery
    ON policy_news_clusters (primary_topic, delivery_class, first_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_news_clusters_search
    ON policy_news_clusters
    USING GIN (to_tsvector(
        'english',
        coalesce(canonical_title, '') || ' ' ||
        coalesce(what_happened, '') || ' ' ||
        coalesce(why_it_matters, '') || ' ' ||
        coalesce(reason_for_inclusion, '')
    ));

CREATE TABLE IF NOT EXISTS policy_news_articles (
    article_key TEXT PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES policy_news_sources(source_key) ON DELETE CASCADE,
    external_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    raw_url TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    content_text TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ,
    categories JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    classification_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    excerpt_only BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ,
    cluster_id TEXT REFERENCES policy_news_clusters(cluster_id) ON DELETE SET NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policy_news_articles_processed
    ON policy_news_articles (processed_at, ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_news_articles_cluster
    ON policy_news_articles (cluster_id, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_news_articles_search
    ON policy_news_articles
    USING GIN (to_tsvector(
        'english',
        coalesce(title, '') || ' ' ||
        coalesce(excerpt, '') || ' ' ||
        coalesce(content_text, '')
    ));

CREATE TABLE IF NOT EXISTS policy_news_cluster_articles (
    cluster_id TEXT NOT NULL REFERENCES policy_news_clusters(cluster_id) ON DELETE CASCADE,
    article_key TEXT NOT NULL REFERENCES policy_news_articles(article_key) ON DELETE CASCADE,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    similarity DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, article_key)
);

CREATE INDEX IF NOT EXISTS idx_policy_news_cluster_articles_article
    ON policy_news_cluster_articles (article_key);

CREATE TABLE IF NOT EXISTS policy_news_alerts (
    alert_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL REFERENCES policy_news_clusters(cluster_id) ON DELETE CASCADE,
    slack_channel_id TEXT NOT NULL,
    slack_thread_ts TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivery_class TEXT NOT NULL,
    message_text TEXT NOT NULL,
    reason_for_inclusion TEXT NOT NULL DEFAULT '',
    score_total INTEGER NOT NULL DEFAULT 0,
    last_scanned_reply_ts TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policy_news_alerts_cluster_sent
    ON policy_news_alerts (cluster_id, sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_news_alerts_thread
    ON policy_news_alerts (slack_channel_id, slack_thread_ts);

CREATE TABLE IF NOT EXISTS policy_news_feedback (
    id BIGSERIAL PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES policy_news_alerts(alert_id) ON DELETE CASCADE,
    slack_channel_id TEXT NOT NULL,
    slack_thread_ts TEXT NOT NULL,
    reply_ts TEXT NOT NULL,
    reply_user TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL,
    command TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (alert_id, reply_ts)
);

CREATE INDEX IF NOT EXISTS idx_policy_news_feedback_alert_created
    ON policy_news_feedback (alert_id, created_at DESC);

-- migrate:down

DROP TABLE IF EXISTS policy_news_feedback;
DROP TABLE IF EXISTS policy_news_alerts;
DROP TABLE IF EXISTS policy_news_cluster_articles;
DROP TABLE IF EXISTS policy_news_articles;
DROP TABLE IF EXISTS policy_news_clusters;
DROP TABLE IF EXISTS policy_news_feed_fetches;
DROP TABLE IF EXISTS policy_news_watch_terms;
DROP TABLE IF EXISTS policy_news_sources;
