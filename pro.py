"""Pro plan entitlement and persistent queue primitives.

This module is intentionally independent from the existing Free/Premium download
handlers. It provides the server-side gates and durable records that Pro UI and
workers can reuse without trusting client-supplied plan flags.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import os
import re
import asyncio
from typing import Iterable

from database import _connect
from logger import log

MAX_BATCH_SIZE = int(os.environ.get("PRO_MAX_BATCH_SIZE", "15"))
MAX_QUEUE_PER_USER = int(os.environ.get("PRO_MAX_QUEUE_PER_USER", "20"))
GLOBAL_MAX_QUEUE = int(os.environ.get("PRO_MAX_QUEUE_SIZE", "200"))
PRO_CONCURRENCY = int(os.environ.get("PRO_MAX_CONCURRENT", "3"))
MAX_RETRIES = int(os.environ.get("PRO_MAX_RETRIES", "3"))

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_pro(user_id: int) -> bool:
    """Server-side entitlement check; active Premium also unlocks Pro tools.

    Premium and Pro are separate products in the UI, but bulk download is a
    paid capability. Existing paid Premium users must not be incorrectly
    denied access just because they predate the Pro table migration.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT status, expires_at FROM pro_entitlements WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row and row["status"] == "active" and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc):
                return True
            premium = conn.execute(
                "SELECT expires_at FROM premium WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(premium and datetime.fromisoformat(premium["expires_at"]) > datetime.now(timezone.utc))
    except Exception as exc:
        log.error(f"pro entitlement check failed for {user_id}: {exc}")
        return False


def grant_pro(user_id: int, days: int, granted_by: int, plan: str = "pro") -> bool:
    if days < 1:
        return False
    now = datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            old = conn.execute(
                "SELECT expires_at FROM pro_entitlements WHERE user_id = ?", (user_id,)
            ).fetchone()
            base = now
            if old:
                base = max(base, datetime.fromisoformat(old["expires_at"]))
                conn.execute(
                    """UPDATE pro_entitlements SET plan = ?, status = 'active', expires_at = ?,
                       granted_by = ?, updated_at = ? WHERE user_id = ?""",
                    (plan, (base + timedelta(days=days)).isoformat(), granted_by, _now(), user_id),
                )
            else:
                conn.execute(
                    """INSERT INTO pro_entitlements
                       (user_id, plan, status, started_at, expires_at, granted_by, created_at, updated_at)
                       VALUES (?, ?, 'active', ?, ?, ?, ?, ?)""",
                    (user_id, plan, now.isoformat(), (now + timedelta(days=days)).isoformat(),
                     granted_by, _now(), _now()),
                )
        return True
    except Exception as exc:
        log.error(f"grant_pro failed for {user_id}: {exc}")
        return False


def revoke_pro(user_id: int, revoked_by: int) -> bool:
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE pro_entitlements SET status = 'revoked', updated_at = ?, granted_by = ? WHERE user_id = ?",
                (_now(), revoked_by, user_id),
            )
        return cur.rowcount == 1
    except Exception as exc:
        log.error(f"revoke_pro failed for {user_id}: {exc}")
        return False


def entitlement(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM pro_entitlements WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def parse_batch_urls(text: str) -> tuple[list[str], list[str]]:
    """Return de-duplicated URLs and invalid non-empty lines."""
    urls, invalid, seen = [], [], set()
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        match = URL_RE.search(candidate)
        if not match:
            invalid.append(candidate)
            continue
        url = match.group(0).rstrip(".,)")
        key = url.lower()
        if key not in seen:
            seen.add(key)
            urls.append(url)
    return urls, invalid


def _platform(url: str) -> str:
    value = url.lower()
    if "tiktok.com" in value:
        return "tiktok"
    if "youtube.com" in value or "youtu.be" in value:
        return "youtube"
    if "facebook.com" in value or "fb.watch" in value:
        return "facebook"
    return "unknown"


def create_batch(user_id: int, urls: Iterable[str]) -> dict:
    urls = list(urls)
    if not is_pro(user_id):
        raise PermissionError("Pro plan is required")
    if not urls or len(urls) > MAX_BATCH_SIZE:
        raise ValueError(f"Batch size must be between 1 and {MAX_BATCH_SIZE}")
    now = _now()
    with _connect() as conn:
        queued = conn.execute(
            "SELECT COUNT(*) FROM pro_jobs WHERE user_id = ? AND status IN ('pending','processing')",
            (user_id,),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM pro_jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        if queued + len(urls) > MAX_QUEUE_PER_USER:
            raise ValueError("Your Pro queue is full")
        if total + len(urls) > GLOBAL_MAX_QUEUE:
            raise ValueError("Server queue is full")
        cur = conn.execute(
            "INSERT INTO pro_batches (user_id, status, total, created_at, updated_at) VALUES (?, 'pending', ?, ?, ?)",
            (user_id, len(urls), now, now),
        )
        batch_id = cur.lastrowid
        for url in urls:
            fingerprint = hashlib.sha256(url.encode()).hexdigest()[:16]
            existing = conn.execute(
                "SELECT id FROM pro_jobs WHERE user_id = ? AND url = ? AND status IN ('pending','processing')",
                (user_id, url),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO pro_jobs
                   (batch_id, user_id, url, platform, status, priority, max_attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', 100, ?, ?, ?)""",
                (batch_id, user_id, url, _platform(url), MAX_RETRIES, now, now),
            )
            log.info(f"PRO_JOB_QUEUED job_batch={batch_id} fingerprint={fingerprint}")
    return {"batch_id": batch_id, "total": len(urls)}


def list_jobs(user_id: int, limit: int = 30) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pro_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def cancel_job(user_id: int, job_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE pro_jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'pending'",
            (_now(), job_id, user_id),
        )
    return cur.rowcount == 1


def retry_job(user_id: int, job_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE pro_jobs SET status = 'pending', attempts = 0, error_message = NULL,
               updated_at = ? WHERE id = ? AND user_id = ? AND status = 'failed'""",
            (_now(), job_id, user_id),
        )
    return cur.rowcount == 1


def claim_next_job() -> dict | None:
    """Atomically claim one priority job; worker must enforce global concurrency."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pro_jobs WHERE status = 'pending' ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        now = _now()
        cur = conn.execute(
            "UPDATE pro_jobs SET status = 'processing', attempts = attempts + 1, started_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (now, now, row["id"]),
        )
        if cur.rowcount != 1:
            return None
        result = dict(row)
        result["attempts"] = row["attempts"] + 1
        return result


def finish_job(job_id: int, status: str, error: str = "", result_path: str = "") -> bool:
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("invalid job status")
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM pro_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE pro_jobs SET status = ?, error_message = ?, result_path = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (status, error, result_path, now, now, job_id),
        )
        conn.execute(
            """INSERT INTO pro_history
               (job_id, user_id, url, platform, filename, quality, format, status, error_message, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, row["user_id"], row["url"], row["platform"],
             os.path.basename(result_path) if result_path else None, row["quality"], row["format"],
             status, error, row["created_at"], now),
        )
    return True


def requeue_job(job_id: int, error: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE pro_jobs SET status = 'pending', error_message = ?, updated_at = ? WHERE id = ? AND status = 'processing'",
            (error[:500], _now(), job_id),
        )
    return cur.rowcount == 1


async def worker_loop(processor, stop_event: asyncio.Event | None = None) -> None:
    """Persistent worker; processor(job) must raise on failure and return a path/empty string."""
    semaphore = asyncio.Semaphore(max(1, PRO_CONCURRENCY))
    active: set[asyncio.Task] = set()

    async def run_one(job: dict):
        async with semaphore:
            try:
                result_path = await processor(job)
                finish_job(job["id"], "completed", result_path=result_path or "")
                log.info(f"DOWNLOAD_COMPLETED job_id={job['id']}")
            except asyncio.CancelledError:
                finish_job(job["id"], "cancelled", "Worker cancelled")
                raise
            except Exception as exc:
                message = str(exc)
                if job["attempts"] < job["max_attempts"]:
                    requeue_job(job["id"], message)
                    await asyncio.sleep(min(30, 2 ** job["attempts"]))
                    log.warning(f"PRO_JOB_RETRY job_id={job['id']} attempt={job['attempts']}")
                else:
                    finish_job(job["id"], "failed", message)
                    log.error(f"PRO_JOB_FAILED job_id={job['id']} error={message}")

    while stop_event is None or not stop_event.is_set():
        while len(active) < max(1, PRO_CONCURRENCY):
            job = claim_next_job()
            if not job:
                break
            task = asyncio.create_task(run_one(job))
            active.add(task)
            task.add_done_callback(active.discard)
        if active:
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(1.5)
    if active:
        await asyncio.gather(*active, return_exceptions=True)


def pro_stats() -> dict:
    with _connect() as conn:
        def count(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "active_pro": count("SELECT COUNT(*) FROM pro_entitlements WHERE status = 'active' AND expires_at > datetime('now')"),
            "pending": count("SELECT COUNT(*) FROM pro_jobs WHERE status = 'pending'"),
            "processing": count("SELECT COUNT(*) FROM pro_jobs WHERE status = 'processing'"),
            "failed": count("SELECT COUNT(*) FROM pro_jobs WHERE status = 'failed'"),
            "enhancement": count("SELECT COUNT(*) FROM pro_enhancement_jobs WHERE status IN ('pending','processing')"),
        }


def create_enhancement_job(user_id: int, mode: str, preset: str, input_path: str) -> int:
    if not is_pro(user_id):
        raise PermissionError("Pro plan is required")
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO pro_enhancement_jobs
               (job_id, user_id, mode, preset, status, input_path, created_at)
               VALUES (0, ?, ?, ?, 'pending', ?, ?)""",
            (user_id, mode, preset, input_path, now),
        )
    return cur.lastrowid


def finish_enhancement_job(job_id: int, status: str, output_path: str = "", error: str = "") -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE pro_enhancement_jobs SET status = ?, output_path = ?, error_message = ?,
               completed_at = ? WHERE id = ?""",
            (status, output_path, error[:500], _now(), job_id),
        )
    return cur.rowcount == 1
