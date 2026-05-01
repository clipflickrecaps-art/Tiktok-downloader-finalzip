"""broadcaster.py — Scheduled / repeat broadcast system (Phase 1 + Phase 2).

Phase 1: DB-driven scheduler, send to all users, repeat/pause/cancel.
Phase 2: Per-broadcast auto-delete, delivery tracking, deletion engine.

Two background loops run concurrently:
  • scheduler_loop  — fires pending broadcasts every SCHEDULER_INTERVAL s
  • deletion_loop   — deletes pending delivery messages every DELETION_INTERVAL s

All DB operations are synchronous (SQLite); bot operations are async (aiogram).
Only messages recorded in broadcast_deliveries are ever deleted.
"""

import asyncio
from datetime import datetime, timezone, timedelta

from database import _connect
from logger import log

SCHEDULER_INTERVAL = 30   # seconds between scheduler checks
DELETION_INTERVAL  = 30   # seconds between deletion engine checks
SEND_DELAY         = 0.05  # seconds between per-user sends (flood prevention)
DELETE_DELAY       = 0.05  # seconds between per-message deletions


# ─── Broadcast DB helpers ─────────────────────────────────────────────────────

def create_broadcast(
    btype: str,
    content: str,
    media_file_id: str | None,
    schedule_time: datetime,
    repeat_interval: int | None,
    created_by: int,
    auto_delete_enabled: bool = False,
    auto_delete_after_seconds: int | None = None,
) -> int | None:
    """Insert a new broadcast row. Returns new row id or None on error."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO broadcasts
                   (type, content, media_file_id, schedule_time,
                    repeat_interval_seconds, is_active, is_paused,
                    created_by, created_at, last_run_at, next_run_at,
                    auto_delete_enabled, auto_delete_after_seconds)
                   VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, NULL, ?, ?, ?)""",
                (
                    btype,
                    content or "",
                    media_file_id,
                    schedule_time.isoformat(),
                    repeat_interval,
                    created_by,
                    now,
                    schedule_time.isoformat(),
                    1 if auto_delete_enabled else 0,
                    auto_delete_after_seconds,
                ),
            )
            row_id = cur.lastrowid
        ad_info = (
            f"auto_delete={auto_delete_after_seconds}s"
            if auto_delete_enabled else "no_auto_delete"
        )
        log.info(
            f"Broadcast created: id={row_id} type={btype} "
            f"repeat={repeat_interval}s scheduled={schedule_time.isoformat()[:19]} {ad_info}"
        )
        return row_id
    except Exception as e:
        log.error(f"create_broadcast failed: {e}")
        return None


def get_pending_broadcasts() -> list:
    """Active, unpaused broadcasts whose next_run_at has arrived."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT * FROM broadcasts
                   WHERE is_active = 1
                     AND is_paused = 0
                     AND next_run_at <= ?""",
                (now,),
            ).fetchall()
    except Exception as e:
        log.error(f"get_pending_broadcasts failed: {e}")
        return []


def get_all_broadcasts() -> list:
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM broadcasts ORDER BY id DESC"
            ).fetchall()
    except Exception as e:
        log.error(f"get_all_broadcasts failed: {e}")
        return []


def get_broadcast(bid: int):
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM broadcasts WHERE id = ?", (bid,)
            ).fetchone()
    except Exception as e:
        log.error(f"get_broadcast failed for id={bid}: {e}")
        return None


def _mark_sent(bid: int, repeat_interval: int | None) -> None:
    """After a run: advance next_run_at for repeating jobs, or deactivate."""
    now = datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            if repeat_interval:
                next_run = (now + timedelta(seconds=repeat_interval)).isoformat()
                conn.execute(
                    "UPDATE broadcasts SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                    (now.isoformat(), next_run, bid),
                )
                log.info(f"Broadcast {bid} rescheduled → {next_run[:19]} UTC")
            else:
                conn.execute(
                    "UPDATE broadcasts SET last_run_at = ?, is_active = 0 WHERE id = ?",
                    (now.isoformat(), bid),
                )
                log.info(f"Broadcast {bid} completed (one-time, deactivated)")
    except Exception as e:
        log.error(f"_mark_sent failed for broadcast {bid}: {e}")


def pause_broadcast(bid: int) -> bool:
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE broadcasts SET is_paused = 1 WHERE id = ? AND is_active = 1",
                (bid,),
            )
        ok = cur.rowcount > 0
        if ok:
            log.info(f"Broadcast {bid} paused")
        return ok
    except Exception as e:
        log.error(f"pause_broadcast failed for {bid}: {e}")
        return False


def resume_broadcast(bid: int) -> bool:
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE broadcasts SET is_paused = 0 WHERE id = ? AND is_active = 1",
                (bid,),
            )
        ok = cur.rowcount > 0
        if ok:
            log.info(f"Broadcast {bid} resumed")
        return ok
    except Exception as e:
        log.error(f"resume_broadcast failed for {bid}: {e}")
        return False


def cancel_broadcast(bid: int) -> bool:
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE broadcasts SET is_active = 0 WHERE id = ?", (bid,)
            )
        ok = cur.rowcount > 0
        if ok:
            log.info(f"Broadcast {bid} cancelled")
        return ok
    except Exception as e:
        log.error(f"cancel_broadcast failed for {bid}: {e}")
        return False


def get_broadcast_stats() -> dict:
    try:
        with _connect() as conn:
            total  = conn.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM broadcasts WHERE is_active=1 AND is_paused=0"
            ).fetchone()[0]
            paused = conn.execute(
                "SELECT COUNT(*) FROM broadcasts WHERE is_active=1 AND is_paused=1"
            ).fetchone()[0]
            done   = conn.execute(
                "SELECT COUNT(*) FROM broadcasts WHERE is_active=0"
            ).fetchone()[0]
        return {"total": total, "active": active, "paused": paused, "done": done}
    except Exception as e:
        log.error(f"get_broadcast_stats failed: {e}")
        return {"total": 0, "active": 0, "paused": 0, "done": 0}


# ─── Delivery tracking DB helpers ─────────────────────────────────────────────

def create_delivery(
    broadcast_id: int,
    user_id: int,
    message_id: int,
    sent_at: datetime,
    delete_at: datetime | None,
) -> None:
    """Record one successfully delivered broadcast message.

    delete_status is set to 'pending' when auto-delete is configured,
    or left NULL (skipped) when auto-delete is off.
    """
    status    = "pending" if delete_at else None
    delete_ts = delete_at.isoformat() if delete_at else None
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO broadcast_deliveries
                   (broadcast_id, user_id, message_id, sent_at,
                    delete_at, deleted_at, delete_status, delete_error)
                   VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)""",
                (broadcast_id, user_id, message_id,
                 sent_at.isoformat(), delete_ts, status),
            )
        if delete_at:
            log.debug(
                f"Delivery recorded: broadcast={broadcast_id} user={user_id} "
                f"msg={message_id} delete_at={delete_ts[:19]}"
            )
    except Exception as e:
        log.error(
            f"create_delivery failed for broadcast={broadcast_id} "
            f"user={user_id} msg={message_id}: {e}"
        )


def get_pending_deletions() -> list:
    """Delivery records that are due for deletion right now."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            return conn.execute(
                """SELECT * FROM broadcast_deliveries
                   WHERE delete_status = 'pending'
                     AND delete_at <= ?
                     AND deleted_at IS NULL""",
                (now,),
            ).fetchall()
    except Exception as e:
        log.error(f"get_pending_deletions failed: {e}")
        return []


def mark_deleted(delivery_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                """UPDATE broadcast_deliveries
                   SET deleted_at = ?, delete_status = 'deleted'
                   WHERE id = ?""",
                (now, delivery_id),
            )
    except Exception as e:
        log.error(f"mark_deleted failed for delivery {delivery_id}: {e}")


def mark_failed_deletion(delivery_id: int, error: str) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """UPDATE broadcast_deliveries
                   SET delete_status = 'failed', delete_error = ?
                   WHERE id = ?""",
                (error[:500], delivery_id),
            )
    except Exception as e:
        log.error(f"mark_failed_deletion failed for delivery {delivery_id}: {e}")


def get_delivery_stats(bid: int) -> dict:
    """Per-broadcast delivery summary."""
    try:
        with _connect() as conn:
            sent    = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE broadcast_id = ?", (bid,)
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries "
                "WHERE broadcast_id = ? AND delete_status = 'pending'", (bid,)
            ).fetchone()[0]
            deleted = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries "
                "WHERE broadcast_id = ? AND delete_status = 'deleted'", (bid,)
            ).fetchone()[0]
            failed  = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries "
                "WHERE broadcast_id = ? AND delete_status = 'failed'", (bid,)
            ).fetchone()[0]
        return {"sent": sent, "pending": pending, "deleted": deleted, "failed": failed}
    except Exception as e:
        log.error(f"get_delivery_stats failed for bid={bid}: {e}")
        return {"sent": 0, "pending": 0, "deleted": 0, "failed": 0}


def get_global_delete_stats() -> dict:
    """Global auto-delete summary across all broadcasts."""
    try:
        with _connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE delete_status = 'pending'"
            ).fetchone()[0]
            deleted = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE delete_status = 'deleted'"
            ).fetchone()[0]
            failed  = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE delete_status = 'failed'"
            ).fetchone()[0]
            total   = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE delete_status IS NOT NULL"
            ).fetchone()[0]
        return {"total": total, "pending": pending, "deleted": deleted, "failed": failed}
    except Exception as e:
        log.error(f"get_global_delete_stats failed: {e}")
        return {"total": 0, "pending": 0, "deleted": 0, "failed": 0}


# ─── Time/interval parsers ────────────────────────────────────────────────────

def parse_schedule_time(text: str) -> datetime | None:
    """Parse a schedule time string to a UTC datetime.

    Relative shortcuts:  30s  10m  2h  1d
    Absolute (UTC):      YYYY-MM-DD HH:MM   or   YYYY-MM-DD HH:MM:SS
    Returns None on parse failure.
    """
    text = text.strip()
    lo   = text.lower()
    now  = datetime.now(timezone.utc)

    if lo.endswith("s") and lo[:-1].isdigit():
        return now + timedelta(seconds=int(lo[:-1]))
    if lo.endswith("m") and lo[:-1].isdigit():
        return now + timedelta(minutes=int(lo[:-1]))
    if lo.endswith("h") and lo[:-1].isdigit():
        return now + timedelta(hours=int(lo[:-1]))
    if lo.endswith("d") and lo[:-1].isdigit():
        return now + timedelta(days=int(lo[:-1]))

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_interval(text: str) -> int | None:
    """Parse a repeat interval string to total seconds.

    Accepts: 30s  5m  2h  1d  or plain integer (seconds).
    Returns None on parse failure.
    """
    text = text.strip().lower()
    if text.endswith("s") and text[:-1].isdigit():
        return int(text[:-1])
    if text.endswith("m") and text[:-1].isdigit():
        return int(text[:-1]) * 60
    if text.endswith("h") and text[:-1].isdigit():
        return int(text[:-1]) * 3600
    if text.endswith("d") and text[:-1].isdigit():
        return int(text[:-1]) * 86400
    if text.isdigit():
        return int(text)
    return None


# ─── Sending logic ────────────────────────────────────────────────────────────

async def send_broadcast_to_all(bot, bcast) -> tuple[int, int]:
    """Deliver a broadcast to every registered user.

    Creates a delivery record for every successfully sent message.
    If auto_delete_enabled, calculates delete_at = sent_at + delay and sets
    delete_status='pending' so the deletion_loop picks it up later.

    Returns (sent_count, fail_count).
    """
    import database as _db

    bid          = bcast["id"]
    btype        = bcast["type"]
    content      = bcast["content"] or ""
    file_id      = bcast["media_file_id"]
    repeat       = bcast["repeat_interval_seconds"]
    ad_enabled   = bool(bcast.get("auto_delete_enabled", 0))
    ad_delay     = bcast.get("auto_delete_after_seconds")

    users = _db.get_all_users()
    sent = fail = 0

    log.info(
        f"Broadcast {bid} starting — type={btype} recipients={len(users)} "
        f"auto_delete={'on' if ad_enabled else 'off'}"
    )

    for uid_str in users:
        try:
            uid     = int(uid_str)
            sent_at = datetime.now(timezone.utc)
            msg     = None

            if btype == "text":
                msg = await bot.send_message(uid, content)
            elif btype == "photo":
                msg = await bot.send_photo(uid, photo=file_id, caption=content or None)
            elif btype == "video":
                msg = await bot.send_video(uid, video=file_id, caption=content or None)

            if msg:
                delete_at = (
                    sent_at + timedelta(seconds=ad_delay)
                    if ad_enabled and ad_delay else None
                )
                create_delivery(bid, uid, msg.message_id, sent_at, delete_at)
                if delete_at:
                    log.debug(
                        f"Broadcast {bid}: auto-delete scheduled for user {uid} "
                        f"msg {msg.message_id} at {delete_at.isoformat()[:19]}"
                    )
            sent += 1

        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ("forbidden", "blocked", "deactivated", "not found")):
                log.debug(f"Broadcast {bid}: user {uid_str} blocked/inactive — skipping")
            else:
                log.warning(f"Broadcast {bid}: failed for user {uid_str}: {e}")
            fail += 1

        await asyncio.sleep(SEND_DELAY)

    log.info(f"Broadcast {bid} complete — sent={sent} failed={fail}")
    _mark_sent(bid, repeat)
    return sent, fail


# ─── Scheduler loop ───────────────────────────────────────────────────────────

async def scheduler_loop(bot) -> None:
    """Background asyncio task — DB-driven broadcast scheduler.

    Checks every SCHEDULER_INTERVAL seconds.  Restart-safe: all state is in DB.
    Each triggered broadcast runs as its own asyncio task (non-blocking).
    """
    log.info(f"Broadcast scheduler started (check interval={SCHEDULER_INTERVAL}s)")
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL)
        try:
            pending = get_pending_broadcasts()
            if pending:
                log.info(f"Scheduler: {len(pending)} broadcast(s) triggered")
            for bcast in pending:
                asyncio.create_task(send_broadcast_to_all(bot, dict(bcast)))
        except Exception as e:
            log.error(f"Scheduler loop error: {e}")


# ─── Auto-delete engine ───────────────────────────────────────────────────────

async def deletion_loop(bot) -> None:
    """Background asyncio task — deletes broadcast messages when their time comes.

    Only deletes messages recorded in broadcast_deliveries with
    delete_status='pending' and delete_at <= now.  Nothing else is ever touched.

    Handles all Telegram errors gracefully — a failed deletion is marked
    'failed' in the DB and logged; the loop continues with the next record.
    """
    log.info(f"Broadcast deletion engine started (check interval={DELETION_INTERVAL}s)")
    while True:
        await asyncio.sleep(DELETION_INTERVAL)
        try:
            due = get_pending_deletions()
            if due:
                log.info(f"Deletion engine: {len(due)} message(s) to delete")
            for row in due:
                did    = row["id"]
                uid    = row["user_id"]
                mid    = row["message_id"]
                try:
                    await bot.delete_message(chat_id=uid, message_id=mid)
                    mark_deleted(did)
                    log.debug(
                        f"Auto-deleted broadcast msg {mid} for user {uid} "
                        f"(delivery {did})"
                    )
                except Exception as e:
                    err_str = str(e)
                    mark_failed_deletion(did, err_str)
                    log.warning(
                        f"Auto-delete failed for msg {mid} user {uid} "
                        f"(delivery {did}): {err_str}"
                    )
                await asyncio.sleep(DELETE_DELAY)
        except Exception as e:
            log.error(f"Deletion loop error: {e}")


# ─── Admin text formatters ────────────────────────────────────────────────────

def _interval_label(secs: int | None) -> str:
    if not secs:
        return "Once"
    if secs % 86400 == 0:
        return f"{secs // 86400}d"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


def format_broadcast_list(rows: list) -> str:
    if not rows:
        return (
            "📡 <b>Broadcasts</b>\n\n"
            "No broadcasts created yet.\n"
            "Use <code>/broadcast</code> to create one."
        )
    lines = ["📡 <b>Broadcasts</b>\n━━━━━━━━━━━━━━━━"]
    for r in rows:
        if not r["is_active"]:
            status = "✅ Done"
        elif r["is_paused"]:
            status = "⏸ Paused"
        else:
            status = "🟢 Active"
        repeat   = _interval_label(r["repeat_interval_seconds"])
        next_run = (r["next_run_at"] or "—")[:16]
        ad_flag  = r.get("auto_delete_enabled", 0)
        ad_delay = r.get("auto_delete_after_seconds")
        ad_label = f"🗑 {_interval_label(ad_delay)}" if ad_flag and ad_delay else "—"
        lines.append(
            f"{status} <b>[{r['id']}]</b> {r['type'].upper()}\n"
            f"     Next: {next_run} UTC | Every: {repeat} | AutoDel: {ad_label}"
        )
    lines += [
        "━━━━━━━━━━━━━━━━",
        "/pausebroadcast &lt;id&gt;   /resumebroadcast &lt;id&gt;",
        "/cancelbroadcast &lt;id&gt;  /broadcaststats",
        "/broadcastdeliveries &lt;id&gt;",
    ]
    return "\n".join(lines)


def format_broadcast_stats(stats: dict) -> str:
    return (
        "📊 <b>Broadcast Stats</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📋 Total:     <b>{stats['total']}</b>\n"
        f"🟢 Active:    <b>{stats['active']}</b>\n"
        f"⏸ Paused:    <b>{stats['paused']}</b>\n"
        f"✅ Completed: <b>{stats['done']}</b>\n"
        "━━━━━━━━━━━━━━━━"
    )


def format_delivery_stats(bid: int, bcast, stats: dict) -> str:
    ad_flag  = bcast.get("auto_delete_enabled", 0) if bcast else 0
    ad_delay = bcast.get("auto_delete_after_seconds") if bcast else None
    ad_label = (
        f"✅ Enabled ({_interval_label(ad_delay)})"
        if ad_flag and ad_delay else "❌ Disabled"
    )
    return (
        f"📦 <b>Broadcast Deliveries</b> — ID <b>{bid}</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"Auto Delete: {ad_label}\n\n"
        f"📤 Sent:              <b>{stats['sent']}</b>\n"
        f"⏳ Pending deletion:  <b>{stats['pending']}</b>\n"
        f"🗑 Deleted:           <b>{stats['deleted']}</b>\n"
        f"❌ Failed deletion:   <b>{stats['failed']}</b>\n"
        "━━━━━━━━━━━━━━━━"
    )


def format_global_delete_stats(stats: dict) -> str:
    return (
        "🗑 <b>Auto Delete Stats</b> (all broadcasts)\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📋 Total tracked:    <b>{stats['total']}</b>\n"
        f"⏳ Pending:          <b>{stats['pending']}</b>\n"
        f"✅ Deleted:          <b>{stats['deleted']}</b>\n"
        f"❌ Failed:           <b>{stats['failed']}</b>\n"
        "━━━━━━━━━━━━━━━━"
    )
