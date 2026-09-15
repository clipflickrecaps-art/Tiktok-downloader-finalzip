import os
import csv
import shutil
from datetime import datetime, timezone
from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
import database as db
import roles
import settings as st
from logger import log

EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)


# --- Role-aware keyboards ---

def get_admin_keyboard(role: str = roles.OWNER) -> ReplyKeyboardMarkup:
    if role == roles.OWNER:
        buttons = [
            [KeyboardButton(text="📊 Analytics (စစ်ဆေးရန်)")],
            [KeyboardButton(text="🚫 Ban User"),              KeyboardButton(text="🔓 Unban User")],
            [KeyboardButton(text="📤 Export Users"),          KeyboardButton(text="📤 Export Banned")],
            [KeyboardButton(text="📤 Export History"),        KeyboardButton(text="💾 Backup DB")],
            [KeyboardButton(text="⚙️ System Status"),         KeyboardButton(text="👥 Manage Roles")],
            [KeyboardButton(text="📡 Scheduled Jobs"),        KeyboardButton(text="📢 Create Broadcast")],
            [KeyboardButton(text="💎 Premium ပေးမည်"),        KeyboardButton(text="💎 Premium Users")],
            [KeyboardButton(text="💳 Payment Accounts"),       KeyboardButton(text="⭐ Premium Plans")],
            [KeyboardButton(text="🧾 Payment Orders")],
        ]
    elif role == roles.ADMIN:
        buttons = [
            [KeyboardButton(text="📊 Analytics (စစ်ဆေးရန်)")],
            [KeyboardButton(text="🚫 Ban User"),              KeyboardButton(text="🔓 Unban User")],
            [KeyboardButton(text="📤 Export Users"),          KeyboardButton(text="📤 Export Banned")],
            [KeyboardButton(text="📤 Export History"),        KeyboardButton(text="⚙️ System Status")],
            [KeyboardButton(text="📡 Scheduled Jobs"),        KeyboardButton(text="📢 Create Broadcast")],
        ]
    else:
        # SUPPORT — analytics only
        buttons = [
            [KeyboardButton(text="📊 Analytics (စစ်ဆေးရန်)")],
        ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# --- Analytics ---

async def send_analytics(message: types.Message):
    log.info("Generating analytics report")
    stats = db.get_analytics()
    if not stats:
        await message.reply("❌ Analytics ထုတ်ရာတွင် အမှားဖြစ်သွားပါသည်။")
        return

    text = (
        "📊 <b>Bot Analytics</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"👥 စုစုပေါင်းအသုံးပြုသူ:  <b>{stats['total_users']}</b>\n"
        f"🚫 Ban ခံထားသူ:         <b>{stats['total_banned']}</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📥 စုစုပေါင်းဒေါင်းလုဒ်:  <b>{stats['total_dl']}</b>\n"
        f"✅ အောင်မြင်:            <b>{stats['success_dl']}</b>\n"
        f"❌ မအောင်မြင်:           <b>{stats['failed_dl']}</b>\n"
        f"📅 ယနေ့ဒေါင်းလုဒ်:      <b>{stats['today_dl']}</b>\n"
        "━━━━━━━━━━━━━━━━"
    )
    await message.reply(text, parse_mode="HTML")


# --- Role list ---

async def send_role_list(message: types.Message):
    rows = roles.list_roles()
    if not rows:
        await message.reply("ℹ️ Admin role သတ်မှတ်ထားသူ မရှိသေးပါ။")
        return
    lines = ["👥 <b>Admin Role စာရင်း</b>\n━━━━━━━━━━━━━━━━"]
    role_labels = {roles.OWNER: "👑 Owner", roles.ADMIN: "🛠 Admin", roles.SUPPORT: "🎧 Support"}
    for r in rows:
        label = role_labels.get(r["role"], r["role"])
        lines.append(f"{label} — <code>{r['user_id']}</code>")
    lines.append("━━━━━━━━━━━━━━━━")
    await message.reply("\n".join(lines), parse_mode="HTML")


# --- Export helpers ---

def _safe_cleanup(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        log.warning(f"Export cleanup failed for {path}: {e}")


async def export_users_csv(message: types.Message):
    path = os.path.join(EXPORT_DIR, "users_export.csv")
    log.info("Exporting users CSV")
    try:
        rows = db.get_all_users_full()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["user_id", "username", "first_name", "joined_at"])
            for r in rows:
                writer.writerow([r["user_id"], r["username"], r["first_name"], r["joined_at"]])
        count = len(rows)
        log.info(f"Users CSV exported — {count} rows")
        await message.reply_document(
            FSInputFile(path, filename="users_export.csv"),
            caption=f"📤 Users export — {count} ယောက်"
        )
    except Exception as e:
        log.error(f"export_users_csv failed: {e}")
        await message.reply("❌ Users export မအောင်မြင်ပါ။")
    finally:
        _safe_cleanup(path)


async def export_banned_csv(message: types.Message):
    path = os.path.join(EXPORT_DIR, "banned_export.csv")
    log.info("Exporting banned users CSV")
    try:
        rows = db.get_all_banned_full()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["user_id", "banned_at"])
            for r in rows:
                writer.writerow([r["user_id"], r["banned_at"]])
        count = len(rows)
        log.info(f"Banned CSV exported — {count} rows")
        await message.reply_document(
            FSInputFile(path, filename="banned_export.csv"),
            caption=f"📤 Banned users export — {count} ယောက်"
        )
    except Exception as e:
        log.error(f"export_banned_csv failed: {e}")
        await message.reply("❌ Banned export မအောင်မြင်ပါ။")
    finally:
        _safe_cleanup(path)


async def export_history_csv(message: types.Message):
    path = os.path.join(EXPORT_DIR, "history_export.csv")
    log.info("Exporting download history CSV")
    try:
        rows = db.get_download_history(limit=5000)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "user_id", "media_type", "status", "error_reason", "url", "created_at"])
            for r in rows:
                writer.writerow([
                    r["id"], r["user_id"], r["media_type"],
                    r["status"], r["error_reason"], r["url"], r["created_at"]
                ])
        count = len(rows)
        log.info(f"History CSV exported — {count} rows")
        await message.reply_document(
            FSInputFile(path, filename="history_export.csv"),
            caption=f"📤 Download history export — {count} ကြိမ်"
        )
    except Exception as e:
        log.error(f"export_history_csv failed: {e}")
        await message.reply("❌ History export မအောင်မြင်ပါ။")
    finally:
        _safe_cleanup(path)


async def backup_database(message: types.Message):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(EXPORT_DIR, f"bot_data_backup_{ts}.db")
    log.info(f"Creating database backup → {backup_path}")
    try:
        shutil.copy2(db.DB_FILE, backup_path)
        size_kb = round(os.path.getsize(backup_path) / 1024, 1)
        log.info(f"Database backup created — {size_kb} KB")
        await message.reply_document(
            FSInputFile(backup_path, filename=f"bot_data_backup_{ts}.db"),
            caption=f"💾 Database backup ({size_kb} KB)\n🕐 {ts}"
        )
    except Exception as e:
        log.error(f"backup_database failed: {e}")
        await message.reply("❌ Database backup မအောင်မြင်ပါ။")
    finally:
        _safe_cleanup(backup_path)


# --- System status / feature flags (admin panel) ---

async def send_system_status(message: types.Message):
    """Send the feature flags and threshold lock status to admin."""
    log.info("Generating system status report")
    user_count = db.get_total_users()
    text = st.format_system_status(user_count)
    await message.reply(text, parse_mode="HTML")


# --- Payment accounts (admin panel) ---

async def send_payment_accounts(message: types.Message):
    """Send the full payment accounts list to admin."""
    log.info("Listing payment accounts for admin")
    rows = st.list_payment_accounts()
    text = st.format_payment_accounts_text(rows)
    await message.reply(text, parse_mode="HTML")


# --- Premium plans (admin panel) ---

async def send_premium_plans(message: types.Message):
    """Send the full premium plans list to admin."""
    log.info("Listing premium plans for admin")
    rows = st.list_premium_plans()
    text = st.format_premium_plans_text(rows)
    await message.reply(text, parse_mode="HTML")


# --- Active premium users list ---

async def send_active_premium_users(message: types.Message):
    """Send the list of all currently active premium users to admin."""
    log.info("Listing active premium users for admin")
    rows = st.list_active_premium_users()
    if not rows:
        await message.reply("💎 ယခုအချိန် Premium user မရှိသေးပါ။")
        return

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    lines = [f"💎 <b>Active Premium Users ({len(rows)})</b>\n━━━━━━━━━━━━━━━━"]
    for r in rows:
        exp     = datetime.fromisoformat(r["expires_at"])
        days_left = max(0, (exp - now).days)
        name    = r.get("first_name") or r.get("username") or "—"
        plan    = r.get("plan_name") or r.get("reason") or "manual"
        exp_str = exp.strftime("%Y-%m-%d")
        lines.append(
            f"• <code>{r['user_id']}</code> {name}\n"
            f"  Plan: {plan} | Expires: {exp_str} ({days_left}d left)"
        )
    lines.append("━━━━━━━━━━━━━━━━")
    await message.reply("\n".join(lines), parse_mode="HTML")


# --- Legacy stats report (backward compat) ---

async def send_stats_report(message: types.Message):
    await send_analytics(message)
