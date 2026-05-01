from datetime import datetime, timezone
from database import DB_FILE, _connect
from logger import log

# --- Role constants ---
OWNER   = "owner"
ADMIN   = "admin"
SUPPORT = "support"

# --- Permission keys ---
# Current
PERM_ANALYTICS    = "analytics"
PERM_BROADCAST    = "broadcast"
PERM_BAN          = "ban"
PERM_EXPORT       = "export"
PERM_BACKUP       = "backup"
PERM_MANAGE_ROLES = "manage_roles"

# Future placeholders (Steps 8, 10, 15, 16)
PERM_FORCE_JOIN   = "force_join"   # Step 8
PERM_PREMIUM      = "premium"      # Step 10
PERM_BUSINESS     = "business"     # Step 15
PERM_PRO          = "pro"          # Step 16

# --- Permission matrix ---
PERMISSIONS: dict[str, set] = {
    OWNER: {
        PERM_ANALYTICS, PERM_BROADCAST, PERM_BAN, PERM_EXPORT,
        PERM_BACKUP, PERM_MANAGE_ROLES,
        PERM_FORCE_JOIN, PERM_PREMIUM, PERM_BUSINESS, PERM_PRO,
    },
    ADMIN: {
        PERM_ANALYTICS, PERM_BROADCAST, PERM_BAN, PERM_EXPORT,
    },
    SUPPORT: {
        PERM_ANALYTICS,
    },
}

DENIED_MSG    = "⛔ ဤ feature ကို သင် access မရပါ။"
OWNER_ONLY    = "⛔ Owner access သာ ခွင့်ပြုသည်။"


# --- Init ---

def init_roles(owner_id: int):
    try:
        with _connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_roles (
                    user_id  INTEGER PRIMARY KEY,
                    role     TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    added_by INTEGER
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO admin_roles (user_id, role, added_at, added_by) VALUES (?, ?, ?, ?)",
                (owner_id, OWNER, datetime.now(timezone.utc).isoformat(), owner_id)
            )
        log.info(f"Roles initialised — owner: {owner_id}")
    except Exception as e:
        log.error(f"init_roles failed: {e}")


# --- Lookups ---

def get_role(user_id: int):
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT role FROM admin_roles WHERE user_id = ?", (int(user_id),)
            ).fetchone()
        return row["role"] if row else None
    except Exception as e:
        log.error(f"get_role failed for {user_id}: {e}")
        return None


def is_owner(user_id: int) -> bool:
    return get_role(user_id) == OWNER


def is_admin_or_above(user_id: int) -> bool:
    return get_role(user_id) in (OWNER, ADMIN)


def is_support_or_above(user_id: int) -> bool:
    return get_role(user_id) in (OWNER, ADMIN, SUPPORT)


def has_any_role(user_id: int) -> bool:
    return get_role(user_id) is not None


def has_permission(user_id: int, perm: str) -> bool:
    role = get_role(user_id)
    if not role:
        return False
    return perm in PERMISSIONS.get(role, set())


# --- Management ---

def add_role(user_id: int, role: str, added_by: int) -> bool:
    if role not in (OWNER, ADMIN, SUPPORT):
        log.warning(f"add_role: unknown role '{role}'")
        return False
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_roles (user_id, role, added_at, added_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    role     = excluded.role,
                    added_at = excluded.added_at,
                    added_by = excluded.added_by
                """,
                (int(user_id), role, datetime.now(timezone.utc).isoformat(), int(added_by))
            )
        log.info(f"Role '{role}' assigned to {user_id} by {added_by}")
        return True
    except Exception as e:
        log.error(f"add_role failed for {user_id}: {e}")
        return False


def remove_role(user_id: int, removed_by: int) -> str:
    """Returns: 'ok' | 'not_found' | 'last_owner'"""
    uid = int(user_id)
    role = get_role(uid)
    if not role:
        return "not_found"
    if role == OWNER:
        try:
            with _connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM admin_roles WHERE role = 'owner'"
                ).fetchone()[0]
            if count <= 1:
                log.warning(f"Blocked removal of sole owner {uid}")
                return "last_owner"
        except Exception as e:
            log.error(f"remove_role owner-count check failed: {e}")
            return "last_owner"
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM admin_roles WHERE user_id = ?", (uid,))
        log.info(f"Role removed for {uid} (was {role}), removed by {removed_by}")
        return "ok"
    except Exception as e:
        log.error(f"remove_role failed for {uid}: {e}")
        return "error"


def list_roles() -> list:
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT user_id, role, added_at, added_by FROM admin_roles ORDER BY role, added_at"
            ).fetchall()
    except Exception as e:
        log.error(f"list_roles failed: {e}")
        return []
