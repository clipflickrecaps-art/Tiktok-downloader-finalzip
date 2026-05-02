"""settings.py — Feature flags, threshold lock system, and foundation helpers.

All future systems are controlled through feature flags stored in the `settings`
SQLite table.  The threshold lock system prevents enabling a feature before the
required user count is reached.

Current safe defaults (only cooldown is active; everything else defaults OFF):
  cooldown_enabled       = 1   active now
  force_join_enabled     = 0   locked until 500 users
  premium_enabled        = 0   locked until 1000 users
  monetization_enabled   = 0   locked until 1000 users
  business_layer_enabled = 0   locked until 1000 users
  pro_features_enabled   = 0   locked until 1000 users
  sponsor_mode_enabled   = 0   locked until 5000 users
"""

from datetime import datetime, timezone
from database import _connect
from logger import log

# ─── Threshold definitions (users required to unlock each feature) ────────────

THRESHOLDS: dict[str, int] = {
    "force_join_enabled":      500,
    "premium_enabled":        1000,
    "monetization_enabled":   1000,
    "ad_system_enabled":      1000,
    "business_layer_enabled": 1000,
    "pro_features_enabled":   1000,
    "task_system_enabled":    5000,
    "sponsor_mode_enabled":   5000,
}

# Features with no threshold (always eligible to enable/disable freely)
NO_THRESHOLD = {"cooldown_enabled"}

# ─── Default flag values (safe defaults — future systems OFF) ─────────────────

FLAG_DEFAULTS: dict[str, str] = {
    "cooldown_enabled":       "1",
    "force_join_enabled":     "0",
    "premium_enabled":        "0",
    "monetization_enabled":   "0",
    "ad_system_enabled":      "0",
    "business_layer_enabled": "0",
    "pro_features_enabled":   "0",
    "task_system_enabled":    "0",
    "sponsor_mode_enabled":   "0",
}

# Human-readable labels for the admin panel
FLAG_LABELS: dict[str, str] = {
    "cooldown_enabled":       "Cooldown",
    "force_join_enabled":     "Force Join",
    "premium_enabled":        "Premium / VIP",
    "monetization_enabled":   "Monetization (Daily Limit)",
    "ad_system_enabled":      "Ad Unlock System",
    "business_layer_enabled": "Business Layer",
    "pro_features_enabled":   "Pro Features",
    "task_system_enabled":    "Task / Sponsor System",
    "sponsor_mode_enabled":   "Sponsor Mode (Option C)",
}

# Free-download quota per day before ad gate (future monetization)
DAILY_FREE_QUOTA = 3
AD_UNLOCK_BATCH  = 10


# ─── Initialisation ───────────────────────────────────────────────────────────

def init_settings() -> None:
    """Insert default flag values for any key not yet in the DB.

    Safe to call multiple times — only missing keys are inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    raw_defaults = {
        "daily_free_limit": "3",
    }
    try:
        with _connect() as conn:
            for key, value in FLAG_DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
            for key, value in raw_defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
        log.info("Settings initialised (feature flags ready)")
    except Exception as e:
        log.error(f"init_settings failed: {e}")


# ─── Generic flag get / set ───────────────────────────────────────────────────

def get_flag(key: str) -> bool:
    """Return True when the feature flag `key` is enabled (value == '1')."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            default = FLAG_DEFAULTS.get(key, "0")
            log.warning(f"get_flag: unknown key '{key}', returning default '{default}'")
            return default == "1"
        return row["value"] == "1"
    except Exception as e:
        log.error(f"get_flag failed for '{key}': {e}")
        return False


def get_raw(key: str, default: str = "") -> str:
    """Return raw string value for a settings key."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default
    except Exception as e:
        log.error(f"get_raw failed for '{key}': {e}")
        return default


def set_flag(key: str, enabled: bool, changed_by: int = 0) -> None:
    """Unconditionally write a flag value (no threshold check — use can_enable first)."""
    now   = datetime.now(timezone.utc).isoformat()
    value = "1" if enabled else "0"
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )
        action = "enabled" if enabled else "disabled"
        log.info(f"Feature flag '{key}' {action} by admin {changed_by}")
    except Exception as e:
        log.error(f"set_flag failed for '{key}': {e}")


def set_raw(key: str, value: str) -> None:
    """Write an arbitrary string value to a settings key."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )
    except Exception as e:
        log.error(f"set_raw failed for '{key}': {e}")


# ─── Threshold lock system ────────────────────────────────────────────────────

def can_enable(feature_key: str, user_count: int) -> bool:
    """Return True if the feature is eligible to be enabled (user_count meets threshold)."""
    if feature_key in NO_THRESHOLD:
        return True
    threshold = THRESHOLDS.get(feature_key)
    if threshold is None:
        return True  # unknown feature — allow
    return user_count >= threshold


def get_feature_status(feature_key: str, user_count: int) -> dict:
    """Return a status dict for one feature flag.

    Status values:
      'enabled'  — flag is ON
      'disabled' — eligible but currently OFF
      'eligible' — user_count meets threshold but flag is OFF
      'locked'   — user_count below threshold
    """
    enabled   = get_flag(feature_key)
    threshold = THRESHOLDS.get(feature_key)
    eligible  = can_enable(feature_key, user_count)

    if enabled:
        status = "enabled"
    elif threshold is None or feature_key in NO_THRESHOLD:
        status = "disabled"
    elif eligible:
        status = "eligible"  # can turn on, but currently off
    else:
        status = "locked"

    return {
        "key":       feature_key,
        "label":     FLAG_LABELS.get(feature_key, feature_key),
        "status":    status,
        "enabled":   enabled,
        "eligible":  eligible,
        "threshold": threshold,
    }


def get_all_status(user_count: int) -> list[dict]:
    """Return status dicts for all feature flags, ordered for admin display."""
    return [get_feature_status(k, user_count) for k in FLAG_DEFAULTS]


def format_system_status(user_count: int) -> str:
    """Build the admin-panel system status message string."""
    lines = [
        "⚙️ <b>System Status</b>",
        "━━━━━━━━━━━━━━━━",
        f"👥 Total Users: <b>{user_count}</b>",
        "",
        "🔧 <b>Feature Flags:</b>",
    ]

    for fs in get_all_status(user_count):
        label    = fs["label"]
        status   = fs["status"]
        threshold = fs["threshold"]

        if status == "enabled":
            icon = "✅"
            note = "Enabled"
        elif status == "eligible":
            icon = "🟡"
            note = "Eligible — ready to enable"
        elif status == "disabled":
            icon = "⬜"
            note = "Disabled"
        else:
            icon = "🔒"
            note = f"Locked ({threshold:,} users required, {max(0, threshold - user_count):,} more needed)"

        lines.append(f"  {icon} {label}: {note}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("💡 Use <code>/sysset &lt;feature&gt; on|off</code> to toggle eligible features.")
    lines.append("")
    lines.append("<b>Feature keys:</b>")
    for key in FLAG_DEFAULTS:
        lines.append(f"  <code>{key.replace('_enabled','')}</code>")

    return "\n".join(lines)


# ─── Premium helper functions (PREPARE ONLY — not active) ────────────────────

def is_premium_active_system() -> bool:
    """Return True when the premium system has been enabled by admin."""
    return get_flag("premium_enabled")


def grant_premium_admin(user_id: int, days: int, plan_name: str, granted_by: int) -> bool:
    """Admin-controlled premium grant.  Updates premium table directly.

    Returns True on success.  Does NOT check the premium_enabled flag —
    admins can always manually grant premium regardless of system state.
    """
    from datetime import timedelta
    from database import _connect as _c
    now     = datetime.now(timezone.utc)
    expires = now + timedelta(days=days)
    try:
        with _c() as conn:
            existing = conn.execute(
                "SELECT expires_at FROM premium WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing:
                current = datetime.fromisoformat(existing["expires_at"])
                base    = max(now, current)
                expires = base + timedelta(days=days)
                conn.execute(
                    """UPDATE premium
                       SET expires_at = ?, reason = ?, updated_at = ?, granted_by = ?
                       WHERE user_id = ?""",
                    (expires.isoformat(), plan_name, now.isoformat(), granted_by, user_id),
                )
            else:
                conn.execute(
                    """INSERT INTO premium
                       (user_id, expires_at, reason, granted_at, plan_name, granted_by, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, expires.isoformat(), plan_name,
                     now.isoformat(), plan_name, granted_by, now.isoformat()),
                )
        log.info(f"Premium granted: user {user_id} — {days}d '{plan_name}' by admin {granted_by}")
        return True
    except Exception as e:
        log.error(f"grant_premium_admin failed for {user_id}: {e}")
        return False


def revoke_premium(user_id: int, revoked_by: int = 0) -> bool:
    """Remove premium from a user immediately."""
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM premium WHERE user_id = ?", (user_id,))
        log.info(f"Premium revoked for user {user_id} by admin {revoked_by}")
        return True
    except Exception as e:
        log.error(f"revoke_premium failed for {user_id}: {e}")
        return False


def get_premium_status(user_id: int) -> dict:
    """Return premium status dict for a user (used in admin lookups)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT expires_at, reason, plan_name, granted_by, granted_at FROM premium WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return {"is_premium": False, "expires_at": None, "plan_name": None}
        now    = datetime.now(timezone.utc)
        exp    = datetime.fromisoformat(row["expires_at"])
        active = exp > now
        return {
            "is_premium": active,
            "expires_at": exp if active else None,
            "plan_name":  row["plan_name"] or row["reason"],
            "granted_by": row["granted_by"],
            "granted_at": row["granted_at"],
        }
    except Exception as e:
        log.error(f"get_premium_status failed for {user_id}: {e}")
        return {"is_premium": False, "expires_at": None, "plan_name": None}


def list_active_premium_users() -> list:
    """Return all users with currently active (unexpired) premium, sorted by expiry."""
    try:
        now = datetime.now(timezone.utc)
        with _connect() as conn:
            rows = conn.execute(
                """SELECT p.user_id, p.expires_at, p.plan_name, p.reason,
                          p.granted_by, p.granted_at, u.username, u.first_name
                   FROM premium p
                   LEFT JOIN users u ON u.user_id = p.user_id
                   WHERE p.expires_at > ?
                   ORDER BY p.expires_at ASC""",
                (now.isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"list_active_premium_users failed: {e}")
        return []


# ─── Payment account (PREPARE ONLY — admin panel only) ───────────────────────

PAYMENT_KEYS = {
    "wave":  ("payment_wave_name",   "payment_wave_number",   "payment_wave_note"),
    "kpay":  ("payment_kpay_name",   "payment_kpay_number",   "payment_kpay_note"),
    "kbz":   ("payment_kbz_name",    "payment_kbz_number",    "payment_kbz_note"),
    "aya":   ("payment_aya_name",    "payment_aya_number",    "payment_aya_note"),
}


def get_payment_info() -> dict:
    """Return all payment account settings (admin panel only)."""
    result = {}
    for method, (name_k, num_k, note_k) in PAYMENT_KEYS.items():
        result[method] = {
            "name":   get_raw(name_k, ""),
            "number": get_raw(num_k,  ""),
            "note":   get_raw(note_k, ""),
        }
    return result


def set_payment_info(method: str, field: str, value: str) -> bool:
    """Store a payment account field.  method=wave/kpay/kbz/aya, field=name/number/note."""
    keys = PAYMENT_KEYS.get(method)
    if not keys:
        log.warning(f"set_payment_info: unknown method '{method}'")
        return False
    field_map = {"name": keys[0], "number": keys[1], "note": keys[2]}
    db_key = field_map.get(field)
    if not db_key:
        log.warning(f"set_payment_info: unknown field '{field}'")
        return False
    set_raw(db_key, value)
    log.info(f"Payment info updated: method={method} field={field}")
    return True


# ─── Monetization — daily usage quota & ad unlock ────────────────────────────

DEFAULT_DAILY_LIMIT = 3
AD_UNLOCK_AMOUNT    = 10

_USAGE_DEFAULT = {
    "daily_used_count": 0,
    "extra_quota":      0,
    "last_reset_date":  "",
    "total_unlocks":    0,
}


def get_daily_free_limit() -> int:
    """Return the configured daily free download limit (default 3)."""
    try:
        return int(get_raw("daily_free_limit", str(DEFAULT_DAILY_LIMIT)))
    except (ValueError, TypeError):
        return DEFAULT_DAILY_LIMIT


def get_user_usage(user_id: int) -> dict:
    """Return the usage row for a user. Returns safe defaults if missing."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_usage WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return {"user_id": user_id, **_USAGE_DEFAULT}
        return dict(row)
    except Exception as e:
        log.error(f"get_user_usage failed for {user_id}: {e}")
        return {"user_id": user_id, **_USAGE_DEFAULT}


def reset_usage_if_needed(user_id: int) -> bool:
    """Reset daily counts if the calendar date has changed.

    Resets daily_used_count and extra_quota to 0.
    Preserves total_unlocks (lifetime counter).
    Returns True if a reset occurred.
    """
    from datetime import date
    today = date.today().isoformat()
    usage = get_user_usage(user_id)
    if usage["last_reset_date"] == today:
        return False
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO user_usage
                       (user_id, daily_used_count, extra_quota, last_reset_date, total_unlocks)
                   VALUES (?, 0, 0, ?, 0)
                   ON CONFLICT(user_id) DO UPDATE SET
                       daily_used_count = 0,
                       extra_quota      = 0,
                       last_reset_date  = excluded.last_reset_date""",
                (user_id, today),
            )
        log.info(f"Usage reset for user {user_id} (new day: {today})")
        return True
    except Exception as e:
        log.error(f"reset_usage_if_needed failed for {user_id}: {e}")
        return False


def increment_usage(user_id: int) -> dict:
    """Increment daily_used_count by 1.  No-op when monetization is OFF."""
    if not get_flag("monetization_enabled"):
        return get_user_usage(user_id)
    reset_usage_if_needed(user_id)
    from datetime import date
    today = date.today().isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO user_usage
                       (user_id, daily_used_count, extra_quota, last_reset_date, total_unlocks)
                   VALUES (?, 1, 0, ?, 0)
                   ON CONFLICT(user_id) DO UPDATE SET
                       daily_used_count = daily_used_count + 1""",
                (user_id, today),
            )
        usage = get_user_usage(user_id)
        log.info(f"Usage incremented for user {user_id}: {usage['daily_used_count']} today")
        return usage
    except Exception as e:
        log.error(f"increment_usage failed for {user_id}: {e}")
        return get_user_usage(user_id)


def add_extra_quota(user_id: int, amount: int = AD_UNLOCK_AMOUNT) -> dict:
    """Grant extra downloads via ad unlock.  Returns updated usage."""
    reset_usage_if_needed(user_id)
    from datetime import date
    today = date.today().isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO user_usage
                       (user_id, daily_used_count, extra_quota, last_reset_date, total_unlocks)
                   VALUES (?, 0, ?, ?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET
                       extra_quota   = extra_quota + ?,
                       total_unlocks = total_unlocks + 1""",
                (user_id, amount, today, amount),
            )
        usage = get_user_usage(user_id)
        log.info(
            f"Extra quota +{amount} granted to user {user_id} "
            f"(total_unlocks={usage['total_unlocks']})"
        )
        return usage
    except Exception as e:
        log.error(f"add_extra_quota failed for {user_id}: {e}")
        return get_user_usage(user_id)


def get_remaining_quota(user_id: int) -> int:
    """Return remaining downloads for the user today.

    Returns -1 (unlimited) when:
      • monetization_enabled flag is OFF, or
      • user has active premium AND premium_enabled flag is ON.
    Returns 0 when blocked.
    """
    if not get_flag("monetization_enabled"):
        return -1
    if premium_bypasses_cooldown(user_id):
        return -1
    reset_usage_if_needed(user_id)
    usage = get_user_usage(user_id)
    total_allowed = get_daily_free_limit() + usage["extra_quota"]
    remaining = total_allowed - usage["daily_used_count"]
    return max(0, remaining)


# ── Backward-compat thin wrappers (kept so nothing breaks if called elsewhere) ─

def get_daily_usage(user_id: int) -> dict:
    """Legacy alias → get_user_usage()."""
    return get_user_usage(user_id)


def increment_download_usage(user_id: int) -> dict:
    """Legacy alias → increment_usage()."""
    return increment_usage(user_id)


def check_ad_requirement(user_id: int) -> bool:
    """Legacy alias — returns True when user has 0 remaining and monetization ON."""
    return get_flag("monetization_enabled") and get_remaining_quota(user_id) == 0


def register_ad_unlock(user_id: int) -> bool:
    """Legacy alias → add_extra_quota()."""
    try:
        add_extra_quota(user_id, AD_UNLOCK_AMOUNT)
        log.info(f"Ad unlock registered for user {user_id} (legacy wrapper)")
        return True
    except Exception as e:
        log.error(f"register_ad_unlock failed for {user_id}: {e}")
        return False


# ─── Sponsor mode (PREPARE ONLY — locked until 5000 users) ───────────────────

SPONSOR_KEYS = ("sponsor_title", "sponsor_text", "sponsor_link", "sponsor_reward")


def get_sponsor_config() -> dict:
    """Return sponsor mode config (admin panel only — not yet active)."""
    return {k: get_raw(k, "") for k in SPONSOR_KEYS}


def set_sponsor_config(field: str, value: str) -> bool:
    """Store a sponsor config field (admin panel only)."""
    if field not in SPONSOR_KEYS:
        log.warning(f"set_sponsor_config: unknown field '{field}'")
        return False
    set_raw(field, value)
    log.info(f"Sponsor config updated: field={field}")
    return True


# ─── Cooldown compatibility hook (PREPARE ONLY) ───────────────────────────────

def premium_bypasses_cooldown(user_id: int) -> bool:
    """Return True when premium users should bypass cooldown.

    Only active when BOTH premium_enabled flag is ON and the user has premium.
    Currently always returns False since premium_enabled defaults to OFF.
    """
    if not get_flag("premium_enabled"):
        return False
    try:
        import referral as ref
        return ref.is_premium(user_id)
    except Exception:
        return False


# ─── Payment account CRUD ─────────────────────────────────────────────────────

def add_payment_account(method_name: str, account_name: str,
                        account_number: str, note: str = "") -> int | None:
    """Insert a new payment account.  Returns the new row id, or None on error."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO payment_accounts
                   (method_name, account_name, account_number, note, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (method_name, account_name, account_number, note, now, now),
            )
            row_id = cur.lastrowid
        log.info(f"Payment account added: id={row_id} method='{method_name}'")
        return row_id
    except Exception as e:
        log.error(f"add_payment_account failed: {e}")
        return None


def list_payment_accounts(active_only: bool = False) -> list:
    """Return all payment accounts.  If active_only=True, only return is_active=1."""
    try:
        with _connect() as conn:
            if active_only:
                return conn.execute(
                    "SELECT * FROM payment_accounts WHERE is_active = 1 ORDER BY id"
                ).fetchall()
            return conn.execute(
                "SELECT * FROM payment_accounts ORDER BY id"
            ).fetchall()
    except Exception as e:
        log.error(f"list_payment_accounts failed: {e}")
        return []


def toggle_payment_account(account_id: int) -> bool | None:
    """Flip the is_active flag.  Returns new state (True=active), or None on error."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT is_active FROM payment_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not row:
                return None
            new_state = 0 if row["is_active"] else 1
            conn.execute(
                "UPDATE payment_accounts SET is_active = ?, updated_at = ? WHERE id = ?",
                (new_state, now, account_id),
            )
        log.info(f"Payment account {account_id} toggled → is_active={new_state}")
        return bool(new_state)
    except Exception as e:
        log.error(f"toggle_payment_account failed for id={account_id}: {e}")
        return None


def delete_payment_account(account_id: int) -> bool:
    """Permanently delete a payment account row."""
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM payment_accounts WHERE id = ?", (account_id,))
            deleted = conn.execute("SELECT changes()").fetchone()[0]
        if deleted:
            log.info(f"Payment account deleted: id={account_id}")
        return bool(deleted)
    except Exception as e:
        log.error(f"delete_payment_account failed for id={account_id}: {e}")
        return False


# ─── Premium plan CRUD ────────────────────────────────────────────────────────

def add_premium_plan(plan_name: str, duration_days: int,
                     price: float, currency: str = "MMK") -> int | None:
    """Insert a new premium plan.  Returns the new row id, or None on error."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO premium_plans
                   (plan_name, duration_days, price, currency, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (plan_name, int(duration_days), float(price), currency, now, now),
            )
            row_id = cur.lastrowid
        log.info(f"Premium plan added: id={row_id} name='{plan_name}' days={duration_days} price={price}{currency}")
        return row_id
    except Exception as e:
        log.error(f"add_premium_plan failed: {e}")
        return None


def list_premium_plans(active_only: bool = False) -> list:
    """Return all premium plans ordered by duration."""
    try:
        with _connect() as conn:
            if active_only:
                return conn.execute(
                    "SELECT * FROM premium_plans WHERE is_active = 1 ORDER BY duration_days"
                ).fetchall()
            return conn.execute(
                "SELECT * FROM premium_plans ORDER BY duration_days"
            ).fetchall()
    except Exception as e:
        log.error(f"list_premium_plans failed: {e}")
        return []


def toggle_premium_plan(plan_id: int) -> bool | None:
    """Flip the is_active flag.  Returns new state (True=active), or None on error."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT is_active FROM premium_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if not row:
                return None
            new_state = 0 if row["is_active"] else 1
            conn.execute(
                "UPDATE premium_plans SET is_active = ?, updated_at = ? WHERE id = ?",
                (new_state, now, plan_id),
            )
        log.info(f"Premium plan {plan_id} toggled → is_active={new_state}")
        return bool(new_state)
    except Exception as e:
        log.error(f"toggle_premium_plan failed for id={plan_id}: {e}")
        return None


def delete_premium_plan(plan_id: int) -> bool:
    """Permanently delete a premium plan."""
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM premium_plans WHERE id = ?", (plan_id,))
            deleted = conn.execute("SELECT changes()").fetchone()[0]
        if deleted:
            log.info(f"Premium plan deleted: id={plan_id}")
        return bool(deleted)
    except Exception as e:
        log.error(f"delete_premium_plan failed for id={plan_id}: {e}")
        return False


def format_payment_accounts_text(rows: list) -> str:
    """Build admin-readable text for the payment accounts list."""
    if not rows:
        return "💳 <b>Payment Accounts</b>\n━━━━━━━━━━━━━━━━\nℹ️ မှတ်တမ်းမရှိသေးပါ။\n/addpay ဖြင့် ထည့်ပါ။"
    lines = ["💳 <b>Payment Accounts</b>", "━━━━━━━━━━━━━━━━"]
    for r in rows:
        active = "✅" if r["is_active"] else "⬜"
        lines.append(
            f"{active} <b>[{r['id']}] {r['method_name']}</b>\n"
            f"    👤 {r['account_name']}\n"
            f"    📱 <code>{r['account_number']}</code>\n"
            f"    📝 {r['note'] or '—'}"
        )
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("🔧 /togglepay &lt;id&gt; • /delpay &lt;id&gt;")
    return "\n".join(lines)


def format_premium_plans_text(rows: list) -> str:
    """Build admin-readable text for the premium plans list."""
    if not rows:
        return "⭐ <b>Premium Plans</b>\n━━━━━━━━━━━━━━━━\nℹ️ Plan မရှိသေးပါ။\n/addplan ဖြင့် ထည့်ပါ။"
    lines = ["⭐ <b>Premium Plans</b>", "━━━━━━━━━━━━━━━━"]
    for r in rows:
        active = "✅" if r["is_active"] else "⬜"
        lines.append(
            f"{active} <b>[{r['id']}] {r['plan_name']}</b>\n"
            f"    📅 {r['duration_days']} day(s)\n"
            f"    💰 {r['price']:,.0f} {r['currency']}"
        )
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("🔧 /toggleplan &lt;id&gt; • /delplan &lt;id&gt;")
    return "\n".join(lines)
