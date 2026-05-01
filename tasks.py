"""tasks.py — Task / Sponsor system (Batch 7).

Admin-created tasks let users unlock extra downloads by completing actions.
Threshold-locked until 5000 users (task_system_enabled flag).

Task types:
  join_channel  — user must join a Telegram channel/group
  visit_link    — user clicks an external link (trust-based)
  custom_task   — freeform instruction (trust-based)

Duplicate rewards are prevented via PRIMARY KEY on (user_id, task_id)
in the user_tasks table.
"""

from datetime import datetime, timezone
from database import _connect
from logger import log

TASK_TYPES = ("join_channel", "visit_link", "custom_task")

TYPE_LABELS = {
    "join_channel": "Join Channel",
    "visit_link":   "Visit Link",
    "custom_task":  "Custom Task",
}


# ─── Task CRUD ────────────────────────────────────────────────────────────────

def create_task(title: str, description: str, task_type: str,
                target: str, reward_amount: int,
                created_by: int = 0) -> int | None:
    """Insert a new task. Returns new row id or None on error."""
    if task_type not in TASK_TYPES:
        log.warning(f"create_task: invalid type '{task_type}'")
        return None
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO tasks
                   (title, description, task_type, target, reward_amount,
                    is_active, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (title, description, task_type, target,
                 max(1, reward_amount), now, created_by),
            )
            row_id = cur.lastrowid
        log.info(f"Task created: id={row_id} type={task_type} reward={reward_amount}")
        return row_id
    except Exception as e:
        log.error(f"create_task failed: {e}")
        return None


def get_all_tasks() -> list:
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM tasks ORDER BY id"
            ).fetchall()
    except Exception as e:
        log.error(f"get_all_tasks failed: {e}")
        return []


def get_active_tasks() -> list:
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE is_active = 1 ORDER BY id"
            ).fetchall()
    except Exception as e:
        log.error(f"get_active_tasks failed: {e}")
        return []


def get_task(task_id: int):
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
    except Exception as e:
        log.error(f"get_task failed for id={task_id}: {e}")
        return None


def toggle_task(task_id: int) -> str | None:
    """Toggle is_active. Returns new state ('active'/'inactive') or None."""
    task = get_task(task_id)
    if not task:
        return None
    new_val = 0 if task["is_active"] else 1
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE tasks SET is_active = ? WHERE id = ?", (new_val, task_id)
            )
        state = "active" if new_val else "inactive"
        log.info(f"Task {task_id} toggled → {state}")
        return state
    except Exception as e:
        log.error(f"toggle_task failed for id={task_id}: {e}")
        return None


def delete_task(task_id: int) -> bool:
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        ok = cur.rowcount > 0
        if ok:
            log.info(f"Task {task_id} deleted")
        return ok
    except Exception as e:
        log.error(f"delete_task failed for id={task_id}: {e}")
        return False


# ─── User-task completion ─────────────────────────────────────────────────────

def has_completed_task(user_id: int, task_id: int) -> bool:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_tasks WHERE user_id = ? AND task_id = ?",
                (user_id, task_id),
            ).fetchone()
        return row is not None
    except Exception as e:
        log.error(f"has_completed_task failed for user {user_id} task {task_id}: {e}")
        return False


def mark_task_complete(user_id: int, task_id: int) -> bool:
    """Record task completion.

    Returns True if recorded (first time).
    Returns False if already completed — caller should NOT give reward again.
    """
    if has_completed_task(user_id, task_id):
        return False
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO user_tasks
                   (user_id, task_id, completed_at, reward_given)
                   VALUES (?, ?, ?, 1)""",
                (user_id, task_id, now),
            )
        log.info(f"Task {task_id} marked complete for user {user_id}")
        return True
    except Exception as e:
        log.error(f"mark_task_complete failed for user {user_id} task {task_id}: {e}")
        return False


def get_user_pending_tasks(user_id: int) -> list:
    """Active tasks NOT yet completed by this user."""
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT t.* FROM tasks t
                   WHERE t.is_active = 1
                     AND NOT EXISTS (
                         SELECT 1 FROM user_tasks ut
                         WHERE ut.user_id = ? AND ut.task_id = t.id
                     )
                   ORDER BY t.id""",
                (user_id,),
            ).fetchall()
    except Exception as e:
        log.error(f"get_user_pending_tasks failed for user {user_id}: {e}")
        return []


# ─── Stats ────────────────────────────────────────────────────────────────────

def get_task_stats() -> list:
    """Completion count per task (for admin /taskstats command)."""
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT t.id, t.title, t.task_type, t.reward_amount,
                          t.is_active, COUNT(ut.user_id) AS completions
                   FROM tasks t
                   LEFT JOIN user_tasks ut ON ut.task_id = t.id
                   GROUP BY t.id
                   ORDER BY t.id""",
            ).fetchall()
    except Exception as e:
        log.error(f"get_task_stats failed: {e}")
        return []


# ─── Text formatters ──────────────────────────────────────────────────────────

def format_tasks_admin_text(rows: list) -> str:
    if not rows:
        return (
            "📋 <b>Tasks</b>\n\n"
            "No tasks created yet.\n"
            "Use <code>/addtask</code> to create one."
        )
    lines = ["📋 <b>Tasks</b>\n━━━━━━━━━━━━━━━━"]
    for r in rows:
        icon  = "✅" if r["is_active"] else "⬜"
        ttype = TYPE_LABELS.get(r["task_type"], r["task_type"])
        lines.append(
            f"{icon} <b>[{r['id']}]</b> {r['title']}\n"
            f"     Type: <code>{ttype}</code>  "
            f"Reward: <b>+{r['reward_amount']}</b>  "
            f"Target: <code>{r['target'] or '—'}</code>"
        )
    lines += [
        "━━━━━━━━━━━━━━━━",
        "/toggletask &lt;id&gt; — enable/disable",
        "/deltask &lt;id&gt; — delete",
        "/taskstats — completion stats",
    ]
    return "\n".join(lines)


def format_task_stats_text(rows: list) -> str:
    if not rows:
        return "📊 <b>Task Stats</b>\n\nNo tasks yet."
    lines = ["📊 <b>Task Stats</b>\n━━━━━━━━━━━━━━━━"]
    for r in rows:
        icon = "✅" if r["is_active"] else "⬜"
        lines.append(
            f"{icon} <b>[{r['id']}]</b> {r['title']}\n"
            f"     Completions: <b>{r['completions']}</b>  "
            f"Reward: +{r['reward_amount']}/user  "
            f"Type: <code>{r['task_type']}</code>"
        )
    lines.append("━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ─── Channel-membership check (used by join_channel tasks) ───────────────────

async def check_channel_membership(bot, user_id: int, target: str) -> bool:
    """Return True if user is a member of the channel/group.

    target  — @username or numeric chat_id string.
    Falls back to False if bot cannot access the chat (private channel where bot
    is not an admin).  In that case the caller should trust-grant the reward.
    """
    try:
        chat_id = int(target) if target.lstrip("-").isdigit() else target
        member  = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception as e:
        log.warning(f"check_channel_membership failed for user {user_id} target {target!r}: {e}")
        return None  # None = unknown (bot can't check — trust user)
