import os
import sqlite3
from datetime import datetime, timezone

from logger import log

DB_FILE = "bot_data.db"
USER_FILE = "users.txt"
BANNED_FILE = "banned.txt"


def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --- Initialisation & Migration ---

def init_db():
    try:
        with _connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id    INTEGER PRIMARY KEY,
                    username   TEXT,
                    first_name TEXT,
                    joined_at  TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id   INTEGER PRIMARY KEY,
                    banned_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    url          TEXT,
                    media_type   TEXT,
                    status       TEXT,
                    error_reason TEXT,
                    created_at   TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_roles (
                    user_id  INTEGER PRIMARY KEY,
                    role     TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    added_by INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    user_id      INTEGER PRIMARY KEY,
                    referred_by  INTEGER NOT NULL,
                    joined_at    TEXT NOT NULL,
                    validated    INTEGER NOT NULL DEFAULT 0,
                    validated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS premium (
                    user_id    INTEGER PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    reason     TEXT,
                    granted_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rewards (
                    user_id     INTEGER NOT NULL,
                    reward_type TEXT NOT NULL,
                    given_at    TEXT NOT NULL,
                    PRIMARY KEY (user_id, reward_type)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_usage (
                    user_id              INTEGER NOT NULL,
                    date                 TEXT NOT NULL,
                    free_download_count  INTEGER NOT NULL DEFAULT 0,
                    ad_unlock_count      INTEGER NOT NULL DEFAULT 0,
                    unlocked_until_count INTEGER NOT NULL DEFAULT 0,
                    ad_required_now      INTEGER NOT NULL DEFAULT 0,
                    updated_at           TEXT NOT NULL,
                    PRIMARY KEY (user_id, date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_accounts (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    method_name    TEXT NOT NULL,
                    account_name   TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    note           TEXT,
                    is_active      INTEGER NOT NULL DEFAULT 1,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS premium_plans (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_name     TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    price         REAL NOT NULL,
                    currency      TEXT NOT NULL DEFAULT 'MMK',
                    is_active     INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_usage (
                    user_id          INTEGER PRIMARY KEY,
                    daily_used_count INTEGER NOT NULL DEFAULT 0,
                    extra_quota      INTEGER NOT NULL DEFAULT 0,
                    last_reset_date  TEXT    NOT NULL DEFAULT '',
                    total_unlocks    INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    title         TEXT    NOT NULL,
                    description   TEXT,
                    task_type     TEXT    NOT NULL,
                    target        TEXT,
                    reward_amount INTEGER NOT NULL DEFAULT 5,
                    is_active     INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT    NOT NULL,
                    created_by    INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_tasks (
                    user_id      INTEGER NOT NULL,
                    task_id      INTEGER NOT NULL,
                    completed_at TEXT    NOT NULL,
                    reward_given INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (user_id, task_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    type                    TEXT    NOT NULL,
                    content                 TEXT,
                    media_file_id           TEXT,
                    schedule_time           TEXT    NOT NULL,
                    repeat_interval_seconds INTEGER,
                    is_active               INTEGER NOT NULL DEFAULT 1,
                    is_paused               INTEGER NOT NULL DEFAULT 0,
                    created_by              INTEGER,
                    created_at              TEXT    NOT NULL,
                    last_run_at             TEXT,
                    next_run_at             TEXT    NOT NULL,
                    auto_delete_enabled     INTEGER NOT NULL DEFAULT 0,
                    auto_delete_after_seconds INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    broadcast_id INTEGER NOT NULL,
                    user_id      INTEGER NOT NULL,
                    message_id   INTEGER NOT NULL,
                    sent_at      TEXT    NOT NULL,
                    delete_at    TEXT,
                    deleted_at   TEXT,
                    delete_status TEXT,
                    delete_error TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS yt_daily_usage (
                    user_id    INTEGER NOT NULL,
                    date       TEXT    NOT NULL,
                    yt_count   INTEGER NOT NULL DEFAULT 0,
                    ad_unlocked INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT    NOT NULL,
                    PRIMARY KEY (user_id, date)
                )
            """)
        _migrate_broadcasts_phase2()
        _migrate_premium_columns()
        log.info("Database initialised (bot_data.db)")
        _migrate_from_txt()
    except Exception as e:
        log.error(f"Database init failed: {e}")


def _migrate_broadcasts_phase2():
    """Add auto-delete columns to broadcasts (Phase 2 migration).

    Silently ignores OperationalError when columns already exist (fresh DB
    gets them from CREATE TABLE; existing DBs get them via ALTER TABLE here).
    """
    new_cols = [
        "ALTER TABLE broadcasts ADD COLUMN auto_delete_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE broadcasts ADD COLUMN auto_delete_after_seconds INTEGER",
    ]
    try:
        with _connect() as conn:
            for stmt in new_cols:
                try:
                    conn.execute(stmt)
                except Exception:
                    pass  # column already exists — safe to ignore
    except Exception as e:
        log.error(f"_migrate_broadcasts_phase2 failed: {e}")


def _migrate_premium_columns():
    """Add extra columns to the premium table that were added in Batch 4.

    SQLite does not support ADD COLUMN IF NOT EXISTS, so we attempt each
    column individually and swallow the OperationalError if it already exists.
    """
    new_cols = [
        "ALTER TABLE premium ADD COLUMN plan_name  TEXT",
        "ALTER TABLE premium ADD COLUMN granted_by INTEGER",
        "ALTER TABLE premium ADD COLUMN updated_at TEXT",
    ]
    try:
        with _connect() as conn:
            for stmt in new_cols:
                try:
                    conn.execute(stmt)
                except Exception:
                    pass  # column already exists — safe to ignore
    except Exception as e:
        log.error(f"_migrate_premium_columns failed: {e}")


def _migrate_from_txt():
    imported_users, imported_bans = 0, 0

    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, "r") as f:
                ids = [line.strip() for line in f if line.strip()]
            with _connect() as conn:
                for uid_str in ids:
                    try:
                        uid = int(uid_str)
                        conn.execute(
                            "INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)",
                            (uid, datetime.now(timezone.utc).isoformat())
                        )
                        imported_users += conn.execute("SELECT changes()").fetchone()[0]
                    except ValueError:
                        pass
            if imported_users:
                log.info(f"Migrated {imported_users} user(s) from {USER_FILE}")
            else:
                log.info(f"{USER_FILE} already migrated — no new users imported")
        except Exception as e:
            log.error(f"Failed to migrate {USER_FILE}: {e}")

    if os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, "r") as f:
                ids = [line.strip() for line in f if line.strip()]
            with _connect() as conn:
                for uid_str in ids:
                    try:
                        uid = int(uid_str)
                        conn.execute(
                            "INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
                            (uid, datetime.now(timezone.utc).isoformat())
                        )
                        imported_bans += conn.execute("SELECT changes()").fetchone()[0]
                    except ValueError:
                        pass
            if imported_bans:
                log.info(f"Migrated {imported_bans} banned user(s) from {BANNED_FILE}")
            else:
                log.info(f"{BANNED_FILE} already migrated — no new bans imported")
        except Exception as e:
            log.error(f"Failed to migrate {BANNED_FILE}: {e}")


# --- User operations ---

def add_user(user_id, username=None, first_name=None):
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username   = COALESCE(excluded.username,   users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name)
                """,
                (int(user_id), username, first_name, datetime.now(timezone.utc).isoformat())
            )
        log.info(f"User registered/updated: {user_id} (@{username})")
    except Exception as e:
        log.error(f"add_user failed for {user_id}: {e}")


def get_all_users():
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [str(row["user_id"]) for row in rows]
    except Exception as e:
        log.error(f"get_all_users failed: {e}")
        return []


def get_all_users_full():
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT user_id, username, first_name, joined_at FROM users ORDER BY joined_at"
            ).fetchall()
    except Exception as e:
        log.error(f"get_all_users_full failed: {e}")
        return []


def get_user(user_id):
    """Return a single user row dict, or None if not found."""
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT user_id, username, first_name, joined_at FROM users WHERE user_id = ?",
                (int(user_id),)
            ).fetchone()
    except Exception as e:
        log.error(f"get_user failed for {user_id}: {e}")
        return None


def get_user_download_count(user_id) -> int:
    """Return total successful downloads for a single user."""
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status = 'success'",
                (int(user_id),)
            ).fetchone()[0]
    except Exception as e:
        log.error(f"get_user_download_count failed for {user_id}: {e}")
        return 0


def get_total_users():
    try:
        with _connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except Exception as e:
        log.error(f"get_total_users failed: {e}")
        return 0


# --- Ban operations ---

def ban_user(user_id):
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
                (int(user_id), datetime.now(timezone.utc).isoformat())
            )
        log.info(f"User banned: {user_id}")
    except Exception as e:
        log.error(f"ban_user failed for {user_id}: {e}")


def unban_user(user_id):
    try:
        with _connect() as conn:
            conn.execute(
                "DELETE FROM banned_users WHERE user_id = ?",
                (int(user_id),)
            )
        log.info(f"User unbanned: {user_id}")
    except Exception as e:
        log.error(f"unban_user failed for {user_id}: {e}")


def is_banned(user_id):
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM banned_users WHERE user_id = ?",
                (int(user_id),)
            ).fetchone()
        return row is not None
    except Exception as e:
        log.error(f"is_banned check failed for {user_id}: {e}")
        return False


def get_all_banned_full():
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT user_id, banned_at FROM banned_users ORDER BY banned_at"
            ).fetchall()
    except Exception as e:
        log.error(f"get_all_banned_full failed: {e}")
        return []


# --- Download history ---

def log_download(user_id, url, media_type, status, error_reason=None):
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO downloads (user_id, url, media_type, status, error_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(user_id), url, media_type, status, error_reason,
                 datetime.now(timezone.utc).isoformat())
            )
    except Exception as e:
        log.error(f"log_download failed: {e}")


def get_download_history(limit=2000):
    try:
        with _connect() as conn:
            return conn.execute(
                """
                SELECT id, user_id, url, media_type, status, error_reason, created_at
                FROM downloads ORDER BY created_at DESC LIMIT ?
                """,
                (limit,)
            ).fetchall()
    except Exception as e:
        log.error(f"get_download_history failed: {e}")
        return []


def get_leaderboard(limit: int = 10) -> list:
    """Return top users by successful download count, joined with first_name."""
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT d.user_id,
                          SUM(CASE WHEN d.status='success' THEN 1 ELSE 0 END) AS success,
                          u.first_name
                   FROM downloads d
                   LEFT JOIN users u ON u.user_id = d.user_id
                   GROUP BY d.user_id
                   ORDER BY success DESC
                   LIMIT ?""",
                (limit,)
            ).fetchall()
    except Exception as e:
        log.error(f"get_leaderboard failed: {e}")
        return []


def get_referral_count(user_id: int) -> int:
    """Return number of users referred by this user."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE referred_by = ?",
                (int(user_id),)
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        log.error(f"get_referral_count failed for {user_id}: {e}")
        return 0


def get_user_download_history(user_id: int, limit: int = 10) -> list:
    """Return last N download records for a specific user."""
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT media_type, status, created_at
                   FROM downloads WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (int(user_id), limit),
            ).fetchall()
    except Exception as e:
        log.error(f"get_user_download_history failed for {user_id}: {e}")
        return []


# --- YouTube daily usage ---

def get_yt_daily_count(user_id: int) -> int:
    """Return today's YouTube download count for a user."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT yt_count FROM yt_daily_usage WHERE user_id=? AND date=?",
                (int(user_id), today)
            ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        log.error(f"get_yt_daily_count failed for {user_id}: {e}")
        return 0


def get_yt_ad_unlocked(user_id: int) -> bool:
    """Return whether the user has used an ad-unlock for YouTube today."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT ad_unlocked FROM yt_daily_usage WHERE user_id=? AND date=?",
                (int(user_id), today)
            ).fetchone()
        return bool(row[0]) if row else False
    except Exception as e:
        log.error(f"get_yt_ad_unlocked failed for {user_id}: {e}")
        return False


def increment_yt_daily(user_id: int) -> None:
    """Increment today's YouTube download counter."""
    today = datetime.now(timezone.utc).date().isoformat()
    now   = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO yt_daily_usage (user_id, date, yt_count, ad_unlocked, updated_at)
                VALUES (?, ?, 1, 0, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    yt_count   = yt_count + 1,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), today, now)
            )
    except Exception as e:
        log.error(f"increment_yt_daily failed for {user_id}: {e}")


def consume_yt_daily(user_id: int, limit: int) -> bool:
    """Atomically consume one YouTube slot when the daily limit allows it."""
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO yt_daily_usage (user_id, date, yt_count, ad_unlocked, updated_at)
                VALUES (?, ?, 1, 0, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    yt_count = yt_count + 1,
                    updated_at = excluded.updated_at
                WHERE yt_count < ?
                """,
                (int(user_id), today, now, int(limit)),
            )
            return conn.execute("SELECT changes()").fetchone()[0] == 1
    except Exception as e:
        log.error(f"consume_yt_daily failed for {user_id}: {type(e).__name__}: {e}")
        return False


def grant_yt_ad_unlock(user_id: int) -> None:
    """Mark that the user has used their ad-unlock for YouTube today."""
    today = datetime.now(timezone.utc).date().isoformat()
    now   = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO yt_daily_usage (user_id, date, yt_count, ad_unlocked, updated_at)
                VALUES (?, ?, 0, 1, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    ad_unlocked = 1,
                    updated_at  = excluded.updated_at
                """,
                (int(user_id), today, now)
            )
    except Exception as e:
        log.error(f"grant_yt_ad_unlock failed for {user_id}: {e}")


# --- Analytics ---

def get_analytics():
    today     = datetime.now(timezone.utc).date().isoformat()
    now_iso   = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            total_users     = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            today_new_users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            total_banned    = conn.execute("SELECT COUNT(*) FROM banned_users").fetchone()[0]
            active_premium  = conn.execute(
                "SELECT COUNT(*) FROM premium WHERE expires_at > ?", (now_iso,)
            ).fetchone()[0]
            total_dl        = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            success_dl      = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE status='success'"
            ).fetchone()[0]
            failed_dl       = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE status='failed'"
            ).fetchone()[0]
            today_dl        = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE created_at LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            today_success   = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE status='success' AND created_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]
            # Top platform today
            plat_rows = conn.execute(
                """SELECT media_type, COUNT(*) AS c FROM downloads
                   WHERE status='success' AND created_at LIKE ?
                   GROUP BY media_type ORDER BY c DESC LIMIT 3""",
                (f"{today}%",)
            ).fetchall()
            # All-time platform breakdown
            all_rows = conn.execute(
                """SELECT media_type, COUNT(*) AS c FROM downloads
                   WHERE status='success'
                   GROUP BY media_type ORDER BY c DESC LIMIT 6"""
            ).fetchall()
            total_refs = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        log.info("Analytics generated")
        return {
            "total_users":      total_users,
            "today_new_users":  today_new_users,
            "total_banned":     total_banned,
            "active_premium":   active_premium,
            "total_dl":         total_dl,
            "success_dl":       success_dl,
            "failed_dl":        failed_dl,
            "today_dl":         today_dl,
            "today_success":    today_success,
            "today_platform":   [(r["media_type"] or "unknown", r["c"]) for r in plat_rows],
            "all_platform":     [(r["media_type"] or "unknown", r["c"]) for r in all_rows],
            "total_refs":       total_refs,
        }
    except Exception as e:
        log.error(f"get_analytics failed: {e}")
        return {}
