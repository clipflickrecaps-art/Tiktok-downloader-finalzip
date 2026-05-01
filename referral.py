"""referral.py — Referral tracking, validation, rewards, and premium system.

Validation rules (ALL must pass for a referral to be counted as valid):
  1. Not a self-referral.
  2. Referred user is unique (no existing referral record).
  3. Referred user completes at least 1 successful download.
  4. Referred account must be >= VALIDATE_MIN_AGE_SEC seconds old at validation time.

Reward thresholds (checked after each validation event):
  3  valid invites → ad_skip_flag stored   (future feature — NOT activated)
  10 valid invites → premium 1 day         (active)
  50 valid invites → premium 30 days       (active)

Premium: persisted with an expiry timestamp; auto-expires when the datetime passes.
Cleanup: pending referrals older than PENDING_EXPIRE_DAYS days → marked invalid (-1).
"""

from datetime import datetime, timezone, timedelta
from database import _connect
from logger import log

# ─── Tuneable constants ────────────────────────────────────────────────────────

VALIDATE_MIN_AGE_SEC = 60     # referred account must be this old before counting
PENDING_EXPIRE_DAYS  = 7      # pending referrals older than this → auto-expire
AD_SKIP_THRESHOLD    = 3      # invites to earn ad-skip flag (future, not active)
PREMIUM_1D_THRESHOLD = 10     # invites to earn premium 1 day
PREMIUM_30D_THRESHOLD = 50    # invites to earn premium 30 days


# ─── Registration ─────────────────────────────────────────────────────────────

def register_referral(user_id: int, inviter_id: int) -> bool:
    """Record that user_id was invited by inviter_id.

    Returns True if the referral was newly recorded.
    Returns False if self-ref, duplicate, or inviter unknown.
    """
    if user_id == inviter_id:
        log.warning(f"Referral: self-ref blocked for user {user_id}")
        return False

    try:
        with _connect() as conn:
            if conn.execute(
                "SELECT 1 FROM referrals WHERE user_id = ?", (user_id,)
            ).fetchone():
                log.info(f"Referral: user {user_id} already has a referral record")
                return False

            if not conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (inviter_id,)
            ).fetchone():
                log.warning(f"Referral: inviter {inviter_id} not found in users")
                return False

            conn.execute(
                """INSERT OR IGNORE INTO referrals
                   (user_id, referred_by, joined_at, validated)
                   VALUES (?, ?, ?, 0)""",
                (user_id, inviter_id, datetime.now(timezone.utc).isoformat()),
            )
            log.info(f"Referral registered: user {user_id} invited by {inviter_id}")
            return True

    except Exception as e:
        log.error(f"register_referral failed: {e}")
        return False


# ─── Validation ───────────────────────────────────────────────────────────────

def validate_referral(user_id: int) -> bool:
    """Validate the pending referral for user_id (called after their first download).

    Returns True if a referral was newly validated (triggers reward check).
    Returns False if no pending referral, already validated, or age check fails.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT referred_by, joined_at
                   FROM referrals
                   WHERE user_id = ? AND validated = 0""",
                (user_id,),
            ).fetchone()

            if not row:
                return False

            inviter_id = row["referred_by"]
            joined_at  = row["joined_at"]

            age_secs = (
                datetime.now(timezone.utc) - datetime.fromisoformat(joined_at)
            ).total_seconds()

            if age_secs < VALIDATE_MIN_AGE_SEC:
                log.info(
                    f"Referral deferred for {user_id}: account only {age_secs:.0f}s old "
                    f"(min {VALIDATE_MIN_AGE_SEC}s)"
                )
                return False

            conn.execute(
                "UPDATE referrals SET validated = 1, validated_at = ? WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )
            log.info(f"Referral validated: user {user_id} → inviter {inviter_id}")

        _check_and_grant_rewards(inviter_id)
        return True

    except Exception as e:
        log.error(f"validate_referral failed for {user_id}: {e}")
        return False


# ─── Rewards ──────────────────────────────────────────────────────────────────

def _check_and_grant_rewards(inviter_id: int) -> None:
    """Check valid invite count for inviter and grant threshold rewards."""
    try:
        with _connect() as conn:
            valid_count = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE referred_by = ? AND validated = 1",
                (inviter_id,),
            ).fetchone()[0]

            log.info(f"Reward check for inviter {inviter_id}: {valid_count} valid referral(s)")

            def already_rewarded(reward_type: str) -> bool:
                return conn.execute(
                    "SELECT 1 FROM rewards WHERE user_id = ? AND reward_type = ?",
                    (inviter_id, reward_type),
                ).fetchone() is not None

            def record_reward(reward_type: str) -> None:
                conn.execute(
                    "INSERT OR IGNORE INTO rewards (user_id, reward_type, given_at) VALUES (?, ?, ?)",
                    (inviter_id, reward_type, datetime.now(timezone.utc).isoformat()),
                )

            now = datetime.now(timezone.utc)

            # 3 invites → ad-skip flag (future, not yet active)
            if valid_count >= AD_SKIP_THRESHOLD and not already_rewarded("ad_skip_3"):
                record_reward("ad_skip_3")
                log.info(f"Ad-skip flag stored for inviter {inviter_id} (future feature)")

            # 10 invites → premium 1 day
            if valid_count >= PREMIUM_1D_THRESHOLD and not already_rewarded("premium_1d"):
                record_reward("premium_1d")
                _grant_premium(conn, inviter_id, now + timedelta(days=1), "referral_10")
                log.info(f"Premium 1 day granted to inviter {inviter_id}")

            # 50 invites → premium 30 days
            if valid_count >= PREMIUM_30D_THRESHOLD and not already_rewarded("premium_30d"):
                record_reward("premium_30d")
                _grant_premium(conn, inviter_id, now + timedelta(days=30), "referral_50")
                log.info(f"Premium 30 days granted to inviter {inviter_id}")

    except Exception as e:
        log.error(f"_check_and_grant_rewards failed for inviter {inviter_id}: {e}")


def _grant_premium(conn, user_id: int, new_expires: datetime, reason: str) -> None:
    """Insert or extend premium. If already premium, extends from the later of now/current expiry."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT expires_at FROM premium WHERE user_id = ?", (user_id,)
    ).fetchone()

    if row:
        current_exp = datetime.fromisoformat(row["expires_at"])
        base        = max(now, current_exp)
        extended    = base + (new_expires - now)
        conn.execute(
            "UPDATE premium SET expires_at = ?, reason = ? WHERE user_id = ?",
            (extended.isoformat(), reason, user_id),
        )
    else:
        conn.execute(
            "INSERT INTO premium (user_id, expires_at, reason, granted_at) VALUES (?, ?, ?, ?)",
            (user_id, new_expires.isoformat(), reason, now.isoformat()),
        )


# ─── Premium queries ──────────────────────────────────────────────────────────

def is_premium(user_id: int) -> bool:
    """Return True if the user has active (unexpired) premium."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM premium WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return False
        return datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
    except Exception as e:
        log.error(f"is_premium failed for {user_id}: {e}")
        return False


def get_premium_expiry(user_id: int):
    """Return premium expiry datetime (UTC) or None if not premium / expired."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM premium WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        exp = datetime.fromisoformat(row["expires_at"])
        return exp if exp > datetime.now(timezone.utc) else None
    except Exception as e:
        log.error(f"get_premium_expiry failed for {user_id}: {e}")
        return None


# ─── Stats & leaderboard ──────────────────────────────────────────────────────

def get_referral_stats(user_id: int) -> dict:
    """Return referral stats for a user acting as inviter."""
    try:
        with _connect() as conn:
            total   = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE referred_by = ?", (user_id,)
            ).fetchone()[0]
            valid   = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE referred_by = ? AND validated = 1",
                (user_id,),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE referred_by = ? AND validated = 0",
                (user_id,),
            ).fetchone()[0]
            reward_rows  = conn.execute(
                "SELECT reward_type FROM rewards WHERE user_id = ?", (user_id,)
            ).fetchall()
            reward_types = {r["reward_type"] for r in reward_rows}

        return {
            "total":          total,
            "valid":          valid,
            "pending":        pending,
            "has_ad_skip":    "ad_skip_3"    in reward_types,
            "has_premium_1d": "premium_1d"   in reward_types,
            "has_premium_30d":"premium_30d"  in reward_types,
        }
    except Exception as e:
        log.error(f"get_referral_stats failed for {user_id}: {e}")
        return {"total": 0, "valid": 0, "pending": 0,
                "has_ad_skip": False, "has_premium_1d": False, "has_premium_30d": False}


def get_leaderboard(limit: int = 10) -> list:
    """Return top inviters ordered by valid referral count."""
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT r.referred_by,
                          COUNT(*) AS valid_count,
                          u.username,
                          u.first_name
                   FROM referrals r
                   LEFT JOIN users u ON u.user_id = r.referred_by
                   WHERE r.validated = 1
                   GROUP BY r.referred_by
                   ORDER BY valid_count DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
    except Exception as e:
        log.error(f"get_leaderboard failed: {e}")
        return []


# ─── Maintenance ──────────────────────────────────────────────────────────────

def cleanup_inactive_referrals(days: int = PENDING_EXPIRE_DAYS) -> int:
    """Mark pending referrals older than `days` days as invalid (-1).

    Returns the number of records expired.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE referrals SET validated = -1 WHERE validated = 0 AND joined_at < ?",
                (cutoff,),
            )
            count = conn.execute("SELECT changes()").fetchone()[0]
        if count:
            log.info(f"Referral cleanup: {count} inactive referral(s) expired")
        return count
    except Exception as e:
        log.error(f"cleanup_inactive_referrals failed: {e}")
        return 0
