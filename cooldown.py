import time
from logger import log

# ── Configurable ─────────────────────────────────────────────────────────────
# Future: admin panel can expose a setter to change COOLDOWN_SECONDS at runtime
COOLDOWN_SECONDS: int = 30

# In-memory store: {user_id: unix_timestamp_of_last_accepted_request}
_cooldowns: dict[int, float] = {}


# ── Bypass logic ─────────────────────────────────────────────────────────────

def should_bypass(user_id: int) -> bool:
    """Returns True when this user should skip cooldown entirely.

    Active bypass rules (all checked):
      1. Role holders (owner / admin / support) — always bypass.
      2. Premium users — only when premium_enabled flag is ON.
         (gated so disabling the flag instantly reverts the bypass)
    """
    import roles
    if roles.has_any_role(user_id):
        return True
    # Premium cooldown bypass — prepared, only active when flag is enabled
    try:
        import settings as st
        if st.premium_bypasses_cooldown(user_id):
            return True
    except Exception:
        pass
    return False


# ── Core helpers ─────────────────────────────────────────────────────────────

def is_on_cooldown(user_id: int, *, override_seconds: int | None = None) -> bool:
    """Returns True if the user must still wait."""
    duration = override_seconds if override_seconds is not None else COOLDOWN_SECONDS
    last = _cooldowns.get(int(user_id))
    if last is None:
        return False
    return (time.time() - last) < duration


def remaining(user_id: int, *, override_seconds: int | None = None) -> int:
    """Returns whole seconds still remaining on the cooldown (0 if none)."""
    duration = override_seconds if override_seconds is not None else COOLDOWN_SECONDS
    last = _cooldowns.get(int(user_id))
    if last is None:
        return 0
    return max(0, int(duration - (time.time() - last)))


def set_cooldown(user_id: int):
    """Stamp the current time for this user — call once a request is accepted."""
    _cooldowns[int(user_id)] = time.time()
    log.info(f"Cooldown set for user {user_id} ({COOLDOWN_SECONDS}s)")


def clear_cooldown(user_id: int):
    """Manually lift the cooldown — useful for error recovery or admin overrides."""
    _cooldowns.pop(int(user_id), None)
    log.info(f"Cooldown cleared for user {user_id}")


def status_text(user_id: int) -> str:
    """Human-readable cooldown status for the 'My Status' panel."""
    if should_bypass(user_id):
        return "ကန့်သတ်မရှိပါ (Staff)"
    secs = remaining(user_id)
    if secs > 0:
        return f"⏳ {secs} စက္ကန့် ကျန်သည်"
    return "✅ အသင့်ဖြစ်သည်"
