import csv
import sqlite3
from pathlib import Path

DB_DIR = Path.home() / ".config" / "social-monitor"
DB_PATH = DB_DIR / "social-monitor.db"


def get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            twitter_handle TEXT,
            linkedin_url TEXT,
            company TEXT,
            role TEXT,
            source TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS person_categories (
            person_id INTEGER,
            category_id INTEGER,
            PRIMARY KEY (person_id, category_id),
            FOREIGN KEY (person_id) REFERENCES people(id),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            platform TEXT NOT NULL DEFAULT 'twitter',
            content TEXT NOT NULL,
            post_url TEXT,
            posted_at TEXT,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(person_id, post_url),
            FOREIGN KEY (person_id) REFERENCES people(id)
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            signal_type TEXT NOT NULL,
            confidence REAL NOT NULL,
            reasoning TEXT,
            notified INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slack_user_id TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(slack_user_id, category_id),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );
    """)
    conn.commit()


def add_person(
    conn: sqlite3.Connection,
    name: str,
    twitter_handle: str | None = None,
    linkedin_url: str | None = None,
    company: str | None = None,
    role: str | None = None,
    source: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO people (name, twitter_handle, linkedin_url, company, role, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, twitter_handle, linkedin_url, company, role, source),
    )
    conn.commit()
    return cur.lastrowid


def add_category(conn: sqlite3.Connection, name: str, description: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO categories (name, description) VALUES (?, ?)",
        (name, description),
    )
    conn.commit()
    return cur.lastrowid


def add_person_to_category(conn: sqlite3.Connection, person_id: int, category_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO person_categories (person_id, category_id) VALUES (?, ?)",
        (person_id, category_id),
    )
    conn.commit()


def get_people(conn: sqlite3.Connection, category: str | None = None) -> list[dict]:
    if category:
        rows = conn.execute(
            """SELECT p.* FROM people p
               JOIN person_categories pc ON p.id = pc.person_id
               JOIN categories c ON pc.category_id = c.id
               WHERE c.name = ?
               ORDER BY p.name""",
            (category,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM people ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_categories(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_people_with_twitter(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM people WHERE twitter_handle IS NOT NULL AND twitter_handle != '' ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def save_post(
    conn: sqlite3.Connection,
    person_id: int,
    content: str,
    post_url: str | None = None,
    posted_at: str | None = None,
    platform: str = "twitter",
) -> int | None:
    cur = conn.execute(
        """INSERT INTO posts (person_id, platform, content, post_url, posted_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(person_id, post_url) DO NOTHING""",
        (person_id, platform, content, post_url, posted_at),
    )
    conn.commit()
    if cur.lastrowid and cur.rowcount > 0:
        return cur.lastrowid
    return None


def get_unprocessed_posts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT p.*, pe.name AS person_name, pe.company AS person_company
           FROM posts p
           JOIN people pe ON p.person_id = pe.id
           LEFT JOIN signals s ON p.id = s.post_id
           WHERE s.id IS NULL
           ORDER BY p.fetched_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def save_signal(
    conn: sqlite3.Connection,
    post_id: int,
    signal_type: str,
    confidence: float,
    reasoning: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO signals (post_id, signal_type, confidence, reasoning)
           VALUES (?, ?, ?, ?)""",
        (post_id, signal_type, confidence, reasoning),
    )
    conn.commit()
    return cur.lastrowid


def get_unnotified_signals(conn: sqlite3.Connection, min_confidence: float = 0.5) -> list[dict]:
    rows = conn.execute(
        """SELECT s.*, p.content AS post_content, p.post_url, p.platform,
                  pe.name AS person_name, pe.twitter_handle, pe.company
           FROM signals s
           JOIN posts p ON s.post_id = p.id
           JOIN people pe ON p.person_id = pe.id
           WHERE s.notified = 0 AND s.confidence >= ?
           ORDER BY s.confidence DESC""",
        (min_confidence,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_signals_notified(conn: sqlite3.Connection, signal_ids: list[int]) -> None:
    if not signal_ids:
        return
    placeholders = ",".join("?" for _ in signal_ids)
    conn.execute(
        f"UPDATE signals SET notified = 1 WHERE id IN ({placeholders})",
        signal_ids,
    )
    conn.commit()


def get_subscriptions(conn: sqlite3.Connection, category_id: int | None = None) -> list[dict]:
    if category_id is not None:
        rows = conn.execute(
            """SELECT s.*, c.name AS category_name
               FROM subscriptions s
               JOIN categories c ON s.category_id = c.id
               WHERE s.category_id = ?
               ORDER BY s.slack_user_id""",
            (category_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.*, c.name AS category_name
               FROM subscriptions s
               JOIN categories c ON s.category_id = c.id
               ORDER BY s.slack_user_id""",
        ).fetchall()
    return [dict(r) for r in rows]


def add_subscription(conn: sqlite3.Connection, slack_user_id: str, category_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO subscriptions (slack_user_id, category_id) VALUES (?, ?)",
        (slack_user_id, category_id),
    )
    conn.commit()
    return cur.lastrowid


def remove_subscription(conn: sqlite3.Connection, slack_user_id: str, category_id: int) -> None:
    conn.execute(
        "DELETE FROM subscriptions WHERE slack_user_id = ? AND category_id = ?",
        (slack_user_id, category_id),
    )
    conn.commit()


def import_people_csv(conn: sqlite3.Connection, csv_path: str, category_name: str) -> int:
    try:
        cat_id = add_category(conn, category_name)
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT id FROM categories WHERE name = ?", (category_name,)).fetchone()
        cat_id = row["id"]

    count = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            person_id = add_person(
                conn,
                name=row.get("name", "").strip(),
                twitter_handle=row.get("twitter_handle", "").strip() or None,
                linkedin_url=row.get("linkedin_url", "").strip() or None,
                company=row.get("company", "").strip() or None,
                role=row.get("role", "").strip() or None,
                source="csv_import",
            )
            add_person_to_category(conn, person_id, cat_id)
            count += 1
    return count
