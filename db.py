import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# P2-21: 실행 디렉터리에 무관하게 프로젝트 루트 기준 절대경로 (DROPSHIP_DB 로 덮어쓰기 가능)
_PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DROPSHIP_DB", str(_PROJECT_ROOT / "dropship.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_sku TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS supplier_products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier      TEXT NOT NULL,
    supplier_pid  TEXT NOT NULL,
    supplier_sku  TEXT NOT NULL,
    supplier_vid  TEXT,
    raw_json      TEXT,
    last_synced   TEXT,
    UNIQUE(supplier, supplier_sku)
);

CREATE TABLE IF NOT EXISTS channel_variants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_sku     TEXT NOT NULL,
    channel           TEXT NOT NULL,
    channel_product_id TEXT NOT NULL,
    channel_variant_id TEXT NOT NULL,
    channel_sku       TEXT,
    inventory_item_id TEXT,
    last_pushed_qty   INTEGER DEFAULT 0,
    UNIQUE(channel, channel_variant_id)
);
CREATE INDEX IF NOT EXISTS idx_cv_sku ON channel_variants(canonical_sku);

CREATE TABLE IF NOT EXISTS mapping (
    canonical_sku TEXT NOT NULL,
    supplier      TEXT NOT NULL,
    supplier_sku  TEXT NOT NULL,
    priority      INTEGER DEFAULT 1,
    verified      INTEGER DEFAULT 0,
    PRIMARY KEY (canonical_sku, supplier, supplier_sku)
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel           TEXT NOT NULL,
    channel_order_id  TEXT NOT NULL,
    channel_order_num TEXT,
    status            TEXT,
    financial_status  TEXT,
    total             TEXT,
    currency          TEXT,
    customer_email    TEXT,
    shipping_address  TEXT,
    preferred_logistic TEXT,
    raw_json          TEXT,
    fetched_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel, channel_order_id)
);

CREATE TABLE IF NOT EXISTS order_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL,
    channel_line_id   TEXT NOT NULL,
    channel_item_name TEXT,
    supplier_sku      TEXT,
    quantity          INTEGER NOT NULL,
    unit_price        TEXT,
    mapped            INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_oi_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS cj_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    local_order_id   INTEGER NOT NULL UNIQUE,
    cj_order_number  TEXT UNIQUE NOT NULL,
    cj_order_id      TEXT,
    status           TEXT DEFAULT 'PENDING',
    tracking_number  TEXT,
    logistic_name    TEXT,
    is_sandbox       INTEGER DEFAULT 0,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel            TEXT NOT NULL,
    channel_comment_id TEXT NOT NULL,
    product_ref        TEXT,
    author             TEXT,
    body               TEXT,
    rating             INTEGER,
    status             TEXT,
    parent_id          TEXT,
    created_at         TEXT,
    raw_json           TEXT,
    UNIQUE(channel, channel_comment_id)
);

CREATE TABLE IF NOT EXISTS comment_replies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id        INTEGER NOT NULL UNIQUE,
    channel           TEXT NOT NULL,
    channel_reply_id  TEXT,
    body              TEXT,
    status            TEXT DEFAULT 'draft',
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL,
    delivery_id  TEXT NOT NULL,
    topic        TEXT,
    raw_json     TEXT,
    received_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    processed    INTEGER DEFAULT 0,
    last_error   TEXT,
    UNIQUE(provider, delivery_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT DEFAULT CURRENT_TIMESTAMP,
    actor         TEXT,
    action        TEXT NOT NULL,
    target        TEXT,
    before_json   TEXT,
    after_json    TEXT,
    request_id    TEXT,
    prev_hash     TEXT,
    entry_hash    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL,
    entry_type    TEXT NOT NULL,
    amount        TEXT NOT NULL,
    currency      TEXT,
    note          TEXT,
    ts            TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS disputes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cj_order_id     TEXT,
    local_order_id  INTEGER,
    cj_dispute_id   TEXT UNIQUE,
    reason          TEXT,
    amount          TEXT,
    status          TEXT DEFAULT 'open',
    raw_json        TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB에 추가 컬럼이 없으면 보충 (idempotent)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(order_items)")}
    if "channel_item_name" not in cols:
        conn.execute("ALTER TABLE order_items ADD COLUMN channel_item_name TEXT")

    wcols = {r["name"] for r in conn.execute("PRAGMA table_info(webhook_events)")}
    if "raw_json" not in wcols:
        conn.execute("ALTER TABLE webhook_events ADD COLUMN raw_json TEXT")
    if "last_error" not in wcols:
        conn.execute("ALTER TABLE webhook_events ADD COLUMN last_error TEXT")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row  # _migrate 의 r["name"] 접근에 필수
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
