import os, asyncio, time
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InputMediaPhoto, FSInputFile,
    WebAppInfo, MenuButtonWebApp, BotCommand,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from keep_alive import keep_alive
import database as db
import admin_panel as admin
import roles
import cooldown as cd
import referral as ref
import settings as st
from logger import log, send_admin_alert
import downloader
from downloader import DownloadError
import tasks as tk
import broadcaster as bc
import facebook_downloader as fb
import youtube_downloader as yt
import miniapp

API_TOKEN  = os.environ['BOT_TOKEN']
ADMIN_ID   = int(os.environ['ADMIN_ID'])
_BOT_START = time.time()          # used to calculate uptime on the status panel

bot = Bot(token=API_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


# ─── FSM: broadcast creation flow ─────────────────────────────────────────────

class FeedbackFlow(StatesGroup):
    waiting = State()


class BroadcastFlow(StatesGroup):
    choosing_type       = State()   # admin picks text / photo / video
    waiting_content     = State()   # admin sends message content
    previewing          = State()   # admin reviews exact preview before proceeding
    choosing_timing     = State()   # admin picks Send Now / Schedule / Repeat
    waiting_schedule    = State()   # admin enters schedule datetime/offset
    waiting_interval    = State()   # admin enters repeat interval
    choosing_autodelete = State()   # admin picks Yes / No for auto-delete
    waiting_ad_delay    = State()   # admin enters auto-delete delay


class YTCookiesFlow(StatesGroup):
    waiting_file = State()   # owner sends cookies.txt document

_processing:    set = set()
_MINI_APP_URL: str = ""   # set once at startup

# ─── YouTube pending requests cache (uid → {url, info}) ──────────────────────
_yt_pending: dict = {}
YT_FREE_DAILY = 1      # free YouTube downloads per day for non-premium users


def _trigger_referral_validation(uid: int) -> None:
    """Fire-and-forget: validate a pending referral for uid after their first download."""
    try:
        validated = ref.validate_referral(uid)
        if validated:
            log.info(f"Referral auto-validated for user {uid} after first download")
    except Exception as e:
        log.error(f"_trigger_referral_validation failed for {uid}: {e}")


# ─── Static text blocks ───────────────────────────────────────────────────────

START_TEXT = (
    "👋 မင်္ဂလာပါ! <b>Video Downloader Bot</b>\n\n"
    "🔥 Watermark မပါတဲ့ TikTok Video Download\n"
    "📘 Facebook Public Video Download\n"
    "🎬 YouTube Video Download + Resolution ရွေးချယ်မှု\n"
    "🖼 YouTube Thumbnail Download\n"
    "🎵 Audio / MP3 Extract\n"
    "⚡ လွယ်ကူမြန်ဆန်စွာ အသုံးပြုနိုင်ပါသည်\n\n"
    "👇 TikTok / Facebook / YouTube link ကို ဒီ chat ထဲပို့လိုက်ပါ"
)

HOWTO_TEXT = (
    "📘 <b>အသုံးပြုနည်း</b>\n"
    "━━━━━━━━━━━━━━━━\n\n"
    "📱 <b>Mini App (အသစ်)</b>\n"
    "Bot မှ 📱 Open App ခလုတ် နှိပ်ပါ\n"
    "• URL ကူးထည့် → Device ထဲ တိုက်ရိုက် ဒေါင်း\n"
    "• TikTok Audio · YouTube Audio · Thumbnail ဒေါင်းနိုင်\n"
    "• ဒေါင်းမှတ်တမ်း · Profile · Quota ကြည့်နိုင်\n\n"
    "💬 <b>Bot (Chat)</b>\n"
    "1️⃣ Video link ကို copy လုပ်ပါ\n"
    "2️⃣ Bot ကို paste လုပ်ပြီး ပေးပို့ပါ\n"
    "3️⃣ Bot မှ ဗီဒီယို chat ထဲ ပေးပို့မည်\n"
    "4️⃣ 🎵 ခလုတ် နှိပ်ပြီး TikTok Audio ဒေါင်းနိုင်သည်\n\n"
    "⌨️ <b>Commands</b>\n"
    "• /myhistory — ကျွန်ုပ်၏ ဒေါင်းမှတ်တမ်း\n"
    "• /quota — ယနေ့ Quota စစ်ကြည့်မည်\n"
    "• /top — 🏆 Top Downloaders Leaderboard\n"
    "• /feedback — 💬 Feedback ပေးပို့မည်\n"
    "• /cancel — လုပ်ဆောင်မှု ဖျက်သိမ်းမည်\n"
    "• /referral — Referral link ရယူမည်\n\n"
    "⏱ <b>Cooldown:</b> တောင်းဆိုမှုတစ်ခုပြီးနောက် "
    f"{cd.COOLDOWN_SECONDS} seconds စောင့်ရသည်\n\n"
    "⚠️ <b>မှတ်ချက်:</b>\n"
    "• TikTok: 50MB ကျော်ပါက direct link ပေးသည်\n"
    "• Facebook: Public video/reel သာ ပံ့ပိုးသည်\n"
    "• YouTube: တစ်ရက်ကို 1 ပုဒ် (Free) | Premium = Unlimited\n"
    "• Private ဗီဒီယို ဒေါင်းမရပါ\n\n"
    "🔗 <b>ပံ့ပိုးသော Links:</b>\n"
    "📌 TikTok — tiktok.com · vm.tiktok.com · vt.tiktok.com\n"
    "📌 Facebook — facebook.com · fb.watch\n"
    "📌 YouTube — youtube.com · youtu.be · youtube.com/shorts"
)

HELP_TEXT = HOWTO_TEXT

ROLE_HELP = (
    "👥 <b>Role Management Commands</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/addadmin &lt;user_id&gt; — Admin ထည့်ရန်\n"
    "/removeadmin &lt;user_id&gt; — Admin ဖျက်ရန်\n"
    "/addsupport &lt;user_id&gt; — Support ထည့်ရန်\n"
    "/removesupport &lt;user_id&gt; — Support ဖျက်ရန်\n"
    "/roles — Role စာရင်းကြည့်ရန်\n\n"
    "⚙️ <b>System Commands (Owner only)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/sysset &lt;feature&gt; on|off — Feature flag toggle\n\n"
    "💳 <b>Payment Account Commands (Owner only)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/addpay &lt;method&gt;|&lt;name&gt;|&lt;number&gt;|[note]\n"
    "/listpay — Payment accounts ကြည့်ရန်\n"
    "/togglepay &lt;id&gt; — Active/Inactive toggle\n"
    "/delpay &lt;id&gt; — Account ဖျက်ရန်\n\n"
    "⭐ <b>Premium Plan Commands (Owner only)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/addplan &lt;name&gt;|&lt;days&gt;|&lt;price&gt;|[currency]\n"
    "/listplan — Plans ကြည့်ရန်\n"
    "/toggleplan &lt;id&gt; — Active/Inactive toggle\n"
    "/delplan &lt;id&gt; — Plan ဖျက်ရန်\n\n"
    "📊 <b>Monetization Commands (Owner only)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/setlimit &lt;n&gt; — Daily free download limit သတ်မှတ်ရန် (default: 3)\n"
    "/sysset monetization_enabled on|off — Quota system ဖွင့်/ပိတ်ရန်\n"
    "/sysset ad_system_enabled on|off — Ad unlock ဖွင့်/ပိတ်ရန်\n\n"
    "🎯 <b>Task System Commands (Owner only — requires 5000 users)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/addtask &lt;title&gt;|&lt;type&gt;|&lt;target&gt;|&lt;reward&gt;[|description]\n"
    "  Types: join_channel | visit_link | custom_task\n"
    "/listtask — Task စာရင်းကြည့်ရန်\n"
    "/toggletask &lt;id&gt; — Task enable/disable\n"
    "/deltask &lt;id&gt; — Task ဖျက်ရန်\n"
    "/taskstats — Task completion stats\n"
    "/sysset task_system_enabled on|off — Task system ဖွင့်/ပိတ်ရန်\n\n"
    "📡 <b>Scheduled Broadcast Commands (Owner / Admin)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/broadcast — Create broadcast (text/photo/video)\n"
    "/listbroadcast — Scheduled job list ကြည့်ရန်\n"
    "/pausebroadcast &lt;id&gt; — Job ကို ခေတ္တရပ်ရန်\n"
    "/resumebroadcast &lt;id&gt; — Job ကို ပြန်လည်စတင်ရန်\n"
    "/cancelbroadcast &lt;id&gt; — Job ကို ပယ်ဖျက်ရန်\n"
    "/broadcaststats — Broadcast statistics\n\n"
    "📦 <b>Broadcast Phase 2 (Owner / Admin)</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "/broadcastdeliveries &lt;id&gt; — Delivery stats for a broadcast\n"
    "/autodeletestats — Global auto-delete summary"
)


# ─── Inline keyboard builders ─────────────────────────────────────────────────

def _start_kb(domain: str = "") -> InlineKeyboardMarkup:
    """Main menu — shown with /start and via Back button."""
    rows = [
        [
            InlineKeyboardButton(text="📘 အသုံးပြုနည်း", callback_data="cb_howto"),
            InlineKeyboardButton(text="👤 ကျွန်ုပ်၏ Status", callback_data="cb_status"),
        ],
        [
            InlineKeyboardButton(text="🎁 Invite / Referral", callback_data="cb_referral"),
        ],
    ]
    if domain:
        rows.append([
            InlineKeyboardButton(
                text="📱 Mini App ဖွင့်မည်",
                web_app=WebAppInfo(url=f"https://{domain}/app"),
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_kb() -> InlineKeyboardMarkup:
    """Single Back button — shown on guide, status, referral pages."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ ပင်မစာမျက်နှာ", callback_data="cb_back")],
    ])


def _add_miniapp_btn(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Append a 'Download via Mini App' button to any existing keyboard."""
    if not _MINI_APP_URL:
        return kb
    rows = list(kb.inline_keyboard) + [[
        InlineKeyboardButton(
            text="📱 Mini App မှ Device ထဲ ဒေါင်းနိုင်သည်",
            web_app=WebAppInfo(url=_MINI_APP_URL),
        )
    ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _main_reply_kb() -> ReplyKeyboardMarkup:
    """Always-visible chat keyboard — row1: guide|status  row2: referral|home  row3: premium  row4: mini-app."""
    rows = [
        [KeyboardButton(text="📘 အသုံးပြုနည်း"),
         KeyboardButton(text="👤 ကျွန်ုပ်၏ Status")],
        [KeyboardButton(text="🎁 Invite / Referral"),
         KeyboardButton(text="🏠 Main Menu")],
        [KeyboardButton(text="⭐ Premium / VIP")],
    ]
    if _REPLIT_DOMAIN:
        rows.append([
            KeyboardButton(
                text="📱 Video Downloader App",
                web_app=WebAppInfo(url=f"https://{_REPLIT_DOMAIN}/app"),
            )
        ])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


# ─── Shared page renderers (used by both text and inline-callback handlers) ───

async def _render_guide(message: types.Message) -> None:
    await message.reply(HOWTO_TEXT, parse_mode="HTML", reply_markup=_back_kb())


async def _render_status(uid: int, message: types.Message) -> None:
    user_row   = db.get_user(uid)
    role       = roles.get_role(uid)
    role_label = role.capitalize() if role else "Free"
    cd_text    = cd.status_text(uid)
    dl_count   = db.get_user_download_count(uid)

    joined = "—"
    if user_row and user_row["joined_at"]:
        joined = user_row["joined_at"][:10]

    usage_section = ""
    if st.get_flag("monetization_enabled"):
        st.reset_usage_if_needed(uid)
        usage       = st.get_user_usage(uid)
        limit       = st.get_daily_free_limit()
        used        = usage["daily_used_count"]
        extra       = usage["extra_quota"]
        remaining   = max(0, (limit + extra) - used)
        total_unlks = usage["total_unlocks"]
        usage_section = (
            "━━━━━━━━━━━━━━━━\n"
            "📊 <b>Daily Usage</b>\n"
            f"  Free Used:      {used} / {limit}\n"
            f"  Extra Unlocked: +{extra}\n"
            f"  Remaining:      {remaining}\n"
            f"  Total Unlocks:  {total_unlks}\n"
        )

    text = (
        "👤 <b>ကျွန်ုပ်၏ Status</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🆔 User ID:      <code>{uid}</code>\n"
        f"📅 Joined:       {joined}\n"
        f"⬇️ Downloads:    {dl_count}\n"
        f"🎭 Plan:         {role_label}\n"
        f"⏱ Cooldown:     {cd_text}\n"
        f"{usage_section}"
        "━━━━━━━━━━━━━━━━"
    )
    await message.reply(text, parse_mode="HTML", reply_markup=_back_kb())


async def _render_referral(uid: int, message: types.Message) -> None:
    bot_info = await bot.get_me()
    ref_link = f"t.me/{bot_info.username}?start=ref_{uid}"

    stats       = ref.get_referral_stats(uid)
    premium_exp = ref.get_premium_expiry(uid)

    # ── Premium status line ────────────────────────────────────────────────
    if premium_exp:
        exp_str     = premium_exp.strftime("%Y-%m-%d %H:%M UTC")
        premium_line = f"⭐ Premium: <b>Active</b> (expires {exp_str})"
    else:
        premium_line = "⭐ Premium: <b>Free</b>"

    # ── Reward progress lines ──────────────────────────────────────────────
    valid = stats["valid"]

    def _prog(threshold: int, label: str) -> str:
        if valid >= threshold:
            return f"✅ {label}"
        return f"⬜ {label} ({valid}/{threshold})"

    reward_lines = (
        _prog(ref.AD_SKIP_THRESHOLD,    "Ad-skip (coming soon)")  + "\n"
        + _prog(ref.PREMIUM_1D_THRESHOLD, "Premium 1 day")         + "\n"
        + _prog(ref.PREMIUM_30D_THRESHOLD,"Premium 30 days")
    )

    text = (
        "🎁 <b>Referral Program</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>သင်၏ Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"📊 <b>Stats</b>\n"
        f"  👥 Total invited:   <b>{stats['total']}</b>\n"
        f"  ✅ Valid referrals: <b>{stats['valid']}</b>\n"
        f"  ⏳ Pending:        <b>{stats['pending']}</b>\n\n"
        f"🏆 <b>Reward Progress</b>\n"
        f"{reward_lines}\n\n"
        f"{premium_line}\n"
        "━━━━━━━━━━━━━━━━\n"
        "💡 Link ကို သူငယ်ချင်းများနှင့် မျှဝေပါ။ "
        "သူတို့ Bot သုံးမှသာ ရေတွက်မည်။"
    )
    await message.reply(text, parse_mode="HTML", reply_markup=_back_kb())


async def _render_premium(uid: int, message: types.Message) -> None:
    """Render the Premium / VIP page for a regular user."""
    user_count      = db.get_total_users()
    premium_enabled = st.get_flag("premium_enabled")
    threshold       = st.THRESHOLDS.get("premium_enabled", 1000)
    premium_exp     = ref.get_premium_expiry(uid)

    if premium_exp:
        exp_str     = premium_exp.strftime("%Y-%m-%d %H:%M UTC")
        status_line = f"⭐ Current Plan: <b>Premium</b>\n⏰ Expires: {exp_str}"
    else:
        status_line = "📋 Current Plan: <b>Free</b>"

    if not premium_enabled or user_count < threshold:
        needed = max(0, threshold - user_count)
        threshold_note = (
            f"ထပ်လိုသည်: <b>{needed:,}</b> ဦး"
            if needed > 0 else "✅ Threshold ရောက်ပြီ"
        )
        text = (
            "⭐ <b>Premium / VIP</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"{status_line}\n\n"
            "🔒 <b>Coming Soon</b>\n"
            f"Premium/VIP ဝန်ဆောင်မှုကို Bot တွင် <b>{threshold:,}</b> ဦးရောက်မှ ဖွင့်ပေးမည်။\n"
            f"ယခု users: <b>{user_count:,}</b> | {threshold_note}\n\n"
            "✨ <b>Premium အကျိုးကျေးဇူးများ (Preview):</b>\n"
            "• ❌ ကြော်ငြာ မပါ (No Ads)\n"
            "• ⚡ Cooldown လျှော့ချ / ကင်းလွတ်\n"
            "• 📈 နေ့စဉ် Quota မြင့်\n"
            "• ⭐ Premium Priority Experience\n\n"
            "ℹ️ ယခုလောလောဆယ် Premium ဝယ်ယူ၍မရသေးပါ။\n"
            "━━━━━━━━━━━━━━━━"
        )
    else:
        plans      = st.list_premium_plans(active_only=True)
        plan_lines = "".join(
            f"• {p['plan_name']} — {p['duration_days']} ရက် — {p['price']:,.0f} {p['currency']}\n"
            for p in plans
        ) or "• (Plans မရှိသေးပါ)\n"

        text = (
            "⭐ <b>Premium / VIP</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"{status_line}\n\n"
            "✨ <b>Premium အကျိုးကျေးဇူးများ:</b>\n"
            "• ❌ ကြော်ငြာ မပါ (No Ads)\n"
            "• ⚡ Cooldown ကင်းလွတ်\n"
            "• 📈 နေ့စဉ် Quota မြင့်\n"
            "• ⭐ Premium Priority Experience\n\n"
            "💳 <b>Premium Plans:</b>\n"
            f"{plan_lines}"
            "━━━━━━━━━━━━━━━━\n"
            "ℹ️ Premium ဝယ်ယူရန် Admin ထံ ဆက်သွယ်ပါ။"
        )

    log.info(f"User {uid} opened Premium page (enabled={premium_enabled}, users={user_count})")
    await message.reply(text, parse_mode="HTML", reply_markup=_back_kb())


# ─── /start ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject):
    uid  = message.from_user.id
    role = roles.get_role(uid)
    if role:
        log.info(f"Role holder ({uid}, {role}) sent /start")
        kbd  = admin.get_admin_keyboard(role)
        text = f"🛠 Admin Panel — <b>{role.capitalize()}</b>"
        await message.reply(text, reply_markup=kbd, parse_mode="HTML")
    else:
        if db.is_banned(uid):
            log.info(f"Banned user {uid} tried /start")
            return
        db.add_user(uid, username=message.from_user.username,
                    first_name=message.from_user.first_name)
        log.info(f"User {uid} sent /start")

        # ── Referral link handling ─────────────────────────────────────────
        arg = (command.args or "").strip()
        if arg.startswith("ref_"):
            try:
                inviter_id = int(arg[4:])
                if ref.register_referral(uid, inviter_id):
                    log.info(f"Referral link used: user {uid} referred by {inviter_id}")
            except ValueError:
                log.warning(f"Invalid ref param from user {uid}: {arg!r}")

        await message.reply(START_TEXT, parse_mode="HTML", reply_markup=_main_reply_kb())


# ─── /help ───────────────────────────────────────────────────────────────────

@dp.message(Command("help"))
async def help_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    log.info(f"User {uid} sent /help")
    await message.reply(HELP_TEXT, parse_mode="HTML", reply_markup=_back_kb())


# ─── Inline button callbacks (user-facing) ────────────────────────────────────

@dp.callback_query(F.data == "cb_howto")
async def cb_howto_handler(call: types.CallbackQuery):
    uid = call.from_user.id
    if db.is_banned(uid):
        return await call.answer("You are banned.", show_alert=True)
    log.info(f"User {uid} opened Guide page (inline)")
    await call.answer()
    await _render_guide(call.message)


@dp.callback_query(F.data == "cb_status")
async def cb_status_handler(call: types.CallbackQuery):
    uid = call.from_user.id
    if db.is_banned(uid):
        return await call.answer("You are banned.", show_alert=True)
    log.info(f"User {uid} opened Status page (inline)")
    await call.answer()
    await _render_status(uid, call.message)


@dp.callback_query(F.data == "cb_referral")
async def cb_referral_handler(call: types.CallbackQuery):
    uid = call.from_user.id
    if db.is_banned(uid):
        return await call.answer("You are banned.", show_alert=True)
    log.info(f"User {uid} opened Referral page (inline)")
    await call.answer()
    await _render_referral(uid, call.message)


@dp.callback_query(F.data == "cb_back")
async def cb_back_handler(call: types.CallbackQuery):
    uid = call.from_user.id
    if db.is_banned(uid):
        return await call.answer("You are banned.", show_alert=True)
    await call.answer()
    await call.message.reply(START_TEXT, parse_mode="HTML", reply_markup=_main_reply_kb())


# ─── Reply keyboard — user menu button handlers ───────────────────────────────

@dp.message(F.text == "📘 အသုံးပြုနည်း")
async def btn_guide_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    log.info(f"User {uid} tapped Guide button")
    await _render_guide(message)


@dp.message(F.text == "👤 ကျွန်ုပ်၏ Status")
async def btn_status_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    log.info(f"User {uid} tapped Status button")
    await _render_status(uid, message)


@dp.message(F.text == "🎁 Invite / Referral")
async def btn_referral_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    log.info(f"User {uid} tapped Referral button")
    await _render_referral(uid, message)


@dp.message(F.text == "🏠 Main Menu")
async def btn_main_menu_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    log.info(f"User {uid} tapped Main Menu button")
    await message.reply(START_TEXT, parse_mode="HTML", reply_markup=_main_reply_kb())


@dp.message(F.text == "⭐ Premium / VIP")
async def btn_premium_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    if roles.has_any_role(uid):
        return await message.reply("⛔ Admin/Staff မှ Premium page ကို ဝင်ရောက်ခွင့် မရှိပါ။")
    await _render_premium(uid, message)


# ─── Admin keyboard — Analytics ──────────────────────────────────────────────

_PLAT_LABEL = {
    "video":          "🎵 TikTok Video",
    "audio":          "🎵 TikTok Audio",
    "youtube_video":  "▶️ YouTube",
    "facebook_video": "📘 Facebook",
    "direct_link":    "🔗 Direct Link",
}

@dp.message(Command("stats"))
async def stats_command_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_ANALYTICS):
        return
    try:
        data    = db.get_analytics()
        total   = data.get("total_dl", 0)
        success = data.get("success_dl", 0)
        failed  = data.get("failed_dl", 0)
        t_dl    = data.get("today_dl", 0)
        t_ok    = data.get("today_success", 0)
        rate    = f"{success/total*100:.1f}%" if total else "—"
        t_rate  = f"{t_ok/t_dl*100:.1f}%" if t_dl else "—"

        # uptime
        elapsed = int(time.time() - _BOT_START)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        uptime  = f"{h}h {m}m {s}s"

        # today platform summary
        today_plat = data.get("today_platform", [])
        if today_plat:
            top_p = today_plat[0]
            top_label = _PLAT_LABEL.get(top_p[0], top_p[0])
            today_plat_line = f"  • Top platform: {top_label} ({top_p[1]:,} ပုဒ်)\n"
        else:
            today_plat_line = ""

        # all-time breakdown
        all_plat = data.get("all_platform", [])
        breakdown = "\n".join(
            f"  • {_PLAT_LABEL.get(t, t)}: {c:,}"
            for t, c in all_plat
        ) or "  • ဒေတာ မရှိသေး"

        text = (
            "📊 <b>Bot Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "👥 <b>Users</b>\n"
            f"  • Total:         {data.get('total_users', 0):,}\n"
            f"  • Today new:     +{data.get('today_new_users', 0):,}\n"
            f"  • ⭐ Premium:    {data.get('active_premium', 0):,} active\n"
            f"  • 🚫 Banned:     {data.get('total_banned', 0):,}\n"
            f"  • 🤝 Referrals:  {data.get('total_refs', 0):,}\n\n"

            "📅 <b>Today</b>\n"
            f"  • Downloads:     {t_dl:,}  (✅ {t_ok:,} · {t_rate})\n"
            f"{today_plat_line}\n"

            "⬇️ <b>All-time Downloads</b>\n"
            f"  • Total:         {total:,}\n"
            f"  • ✅ Success:    {success:,}  ({rate})\n"
            f"  • ❌ Failed:     {failed:,}\n\n"

            "🎯 <b>Platform Breakdown (Success)</b>\n"
            f"{breakdown}\n\n"

            f"⏱ <b>Uptime:</b> {uptime}"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        log.error(f"[stats] error: {e}")
        await message.reply("❌ Stats ထုတ်ရာတွင် အမှားဖြစ်သွားပါသည်")


@dp.message(Command("myhistory"))
async def myhistory_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    items = db.get_user_download_history(uid, 10)
    if not items:
        return await message.reply(
            "📭 ဒေါင်းမှတ်တမ်း မရှိသေးပါ\n\nURL ပေးပို့ပြီး ဒေါင်းကြည့်ပါ"
        )
    icons = {
        "video": "📹 TikTok Video", "audio": "🎵 TikTok Audio",
        "youtube_video": "🎬 YouTube", "facebook_video": "📘 Facebook",
        "direct_link": "🔗 Direct Link",
    }
    lines = []
    for i, r in enumerate(items, 1):
        mtype  = r["media_type"] or "download"
        status = "✅" if r["status"] == "success" else "❌"
        ts     = (r["created_at"] or "")[:16].replace("T", " ")
        label  = icons.get(mtype, f"📥 {mtype}")
        lines.append(f"{i}. {status} {label}\n    <code>{ts} UTC</code>")
    await message.answer(
        "📋 <b>ကျွန်ုပ်၏ ဒေါင်းမှတ်တမ်း</b> (နောက်ဆုံး ၁၀ ပုဒ်)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )


@dp.message(Command("quota"))
async def quota_handler(message: types.Message):
    from datetime import timedelta
    uid     = message.from_user.id
    if db.is_banned(uid):
        return
    is_prem = ref.is_premium(uid)
    monet   = st.get_flag("monetization_enabled")
    if is_prem:
        return await message.reply(
            "💎 <b>Premium User</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            "Quota Unlimited — ဒေါင်းနိုင်သမျှ ဒေါင်းနိုင်သည်",
            parse_mode="HTML",
        )
    if not monet:
        return await message.reply(
            "ℹ️ ယနေ့ Quota limit မရှိသေးပါ\nဒေါင်းနိုင်သမျှ ဒေါင်းနိုင်သည်"
        )
    st.reset_usage_if_needed(uid)
    usg       = st.get_user_usage(uid)
    used      = usg.get("daily_used_count", 0)
    limit     = st.get_daily_free_limit()
    remaining = max(0, limit - used)
    yt_used   = db.get_yt_daily_count(uid)
    from datetime import datetime, timezone, timedelta as _td
    now      = datetime.now(timezone.utc)
    midnight = (now + _td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    diff     = midnight - now
    h, m     = diff.seconds // 3600, (diff.seconds % 3600) // 60
    filled   = int(used / limit * 10) if limit else 0
    bar      = "🟦" * filled + "⬜" * (10 - filled)
    await message.answer(
        f"📊 <b>ယနေ့ Download Quota</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{bar}\n"
        f"✅ ဒေါင်းပြီး: {used} / {limit} ပုဒ်\n"
        f"⏳ ကျန်: <b>{remaining} ပုဒ်</b>\n"
        f"🎬 YouTube ယနေ့: {yt_used} ပုဒ်\n\n"
        f"🔄 Reset: <b>{h}h {m}m</b> ကျန်သည်",
        parse_mode="HTML",
    )


@dp.message(Command("top"))
async def top_handler(message: types.Message):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    rows = db.get_leaderboard(10)
    if not rows:
        return await message.reply("📭 မှတ်တမ်း မရှိသေးပါ")
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = []
    for i, row in enumerate(rows):
        cnt   = row["success"] or 0
        fname = (row["first_name"] or f"User …{str(row['user_id'])[-4:]}")[:22]
        medal = medals[i] if i < len(medals) else f"{i+1}."
        tag   = "  ◀ You" if row["user_id"] == uid else ""
        lines.append(f"{medal} {fname} — {cnt} ပုဒ်{tag}")
    await message.answer(
        "🏆 <b>Top Downloaders</b>\n"
        "━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@dp.message(Command("feedback"))
async def feedback_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if db.is_banned(uid):
        return
    await message.reply(
        "💬 <b>Feedback ပေးပို့ရန်</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "ပြဿနာ · အကြံပြုချက် · မေးမြန်းချင်သည်များ ရေးပါ\n\n"
        "<i>/cancel — မပေးပို့ဘဲ ဖျက်နိုင်သည်</i>",
        parse_mode="HTML",
    )
    await state.set_state(FeedbackFlow.waiting)


@dp.message(FeedbackFlow.waiting, F.text)
async def feedback_receive(message: types.Message, state: FSMContext):
    uid   = message.from_user.id
    fname = message.from_user.full_name or str(uid)
    uname = message.from_user.username or ""
    text  = (message.text or "").strip()
    if not text:
        return await message.reply("⚠️ စာသားရေးပြီးမှ ပေးပို့ပါ")
    await state.clear()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💬 <b>User Feedback</b>\n"
            f"From: {fname}" + (f" (@{uname})" if uname else "") + "\n"
            f"ID: <code>{uid}</code>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"{text[:2000]}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.reply("✅ Feedback ပေးပို့ပြီးပါပြီ။ ကျေးဇူးတင်ပါသည်！")


@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await state.clear()
        await message.reply("❌ ဖျက်ပြီးပါပြီ")


@dp.message(F.text == "📊 Analytics (စစ်ဆေးရန်)")
async def analytics_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_ANALYTICS):
        log.warning(f"Permission denied: analytics for {uid}")
        return await message.reply(roles.DENIED_MSG)
    log.info(f"Analytics requested by {uid} ({roles.get_role(uid)})")
    try:
        await admin.send_analytics(message)
    except Exception as e:
        log.error(f"Analytics failed: {e}")
        await message.reply("❌ Analytics ထုတ်ရာတွင် အမှားဖြစ်သွားပါသည်။")


# ─── Admin keyboard — Premium ဖြန့်ဝေရန် (Owner only) ───────────────────────

@dp.message(F.text == "💎 Premium ပေးမည်")
async def btn_give_premium(message: types.Message):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    await message.reply(
        "💎 <b>Premium ပေးနည်း</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "<code>/givepremium &lt;user_id&gt; &lt;days&gt; [reason]</code>\n\n"
        "ဥပမာ:\n"
        "<code>/givepremium 123456789 30 VIP Gift</code>\n"
        "<code>/givepremium 123456789 7</code>\n\n"
        "⚠️ Threshold မပြည့်ဘဲ တိုက်ရိုက်ပေးနိုင်သည်\n\n"
        "Premium ရုပ်သိမ်းရန်:\n"
        "<code>/revokepremium &lt;user_id&gt;</code>\n\n"
        "User စစ်ကြည့်ရန်:\n"
        "<code>/checkuser &lt;user_id&gt;</code>",
        parse_mode="HTML",
    )


@dp.message(F.text == "💎 Premium Users")
async def btn_premium_users(message: types.Message):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    log.info(f"Owner {uid} viewed premium users list")
    await admin.send_active_premium_users(message)


# ─── Admin keyboard — Ban / Unban ─────────────────────────────────────────────

@dp.message(F.text == "🚫 Ban User")
async def ban_button_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_BAN):
        log.warning(f"Permission denied: ban button for {uid}")
        return await message.reply(roles.DENIED_MSG)
    await message.reply("/ban နောက်မှာ User ID ထည့်ပါ။\nဥပမာ - /ban 12345678")


@dp.message(F.text == "🔓 Unban User")
async def unban_button_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_BAN):
        log.warning(f"Permission denied: unban button for {uid}")
        return await message.reply(roles.DENIED_MSG)
    await message.reply("/unban နောက်မှာ User ID ထည့်ပါ။\nဥပမာ - /unban 12345678")


# ─── Admin keyboard — Exports ────────────────────────────────────────────────

@dp.message(F.text == "📤 Export Users")
async def export_users_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_EXPORT):
        log.warning(f"Permission denied: export users for {uid}")
        return await message.reply(roles.DENIED_MSG)
    log.info(f"Users export requested by {uid}")
    try:
        await admin.export_users_csv(message)
    except Exception as e:
        log.error(f"Export users handler failed: {e}")
        await message.reply("❌ Export မအောင်မြင်ပါ။")


@dp.message(F.text == "📤 Export Banned")
async def export_banned_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_EXPORT):
        log.warning(f"Permission denied: export banned for {uid}")
        return await message.reply(roles.DENIED_MSG)
    log.info(f"Banned export requested by {uid}")
    try:
        await admin.export_banned_csv(message)
    except Exception as e:
        log.error(f"Export banned handler failed: {e}")
        await message.reply("❌ Export မအောင်မြင်ပါ။")


@dp.message(F.text == "📤 Export History")
async def export_history_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_EXPORT):
        log.warning(f"Permission denied: export history for {uid}")
        return await message.reply(roles.DENIED_MSG)
    log.info(f"History export requested by {uid}")
    try:
        await admin.export_history_csv(message)
    except Exception as e:
        log.error(f"Export history handler failed: {e}")
        await message.reply("❌ Export မအောင်မြင်ပါ။")


@dp.message(Command("exporthistory"))
async def cmd_exporthistory(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_EXPORT):
        log.warning(f"Permission denied: /exporthistory for {uid}")
        return await message.reply(roles.DENIED_MSG)
    log.info(f"/exporthistory requested by {uid}")
    try:
        await admin.export_history_csv(message)
    except Exception as e:
        log.error(f"/exporthistory handler failed: {e}")
        await message.reply("❌ Export မအောင်မြင်ပါ။")


# ─── Admin keyboard — Backup ──────────────────────────────────────────────────

@dp.message(F.text == "💾 Backup DB")
async def backup_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_BACKUP):
        log.warning(f"Permission denied: backup for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    log.info(f"DB backup requested by {uid}")
    try:
        await admin.backup_database(message)
    except Exception as e:
        log.error(f"Backup handler failed: {e}")
        await message.reply("❌ Backup မအောင်မြင်ပါ။")


# ─── Admin keyboard — System Status ─────────────────────────────────────────

@dp.message(F.text == "⚙️ System Status")
async def system_status_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_ANALYTICS):
        log.warning(f"Permission denied: system status for {uid}")
        return await message.reply(roles.DENIED_MSG)
    log.info(f"System status requested by {uid}")
    try:
        await admin.send_system_status(message)
    except Exception as e:
        log.error(f"System status handler failed: {e}")
        await message.reply("❌ System status မထုတ်နိုင်ပါ။")


# ─── Admin command — Toggle feature flags ────────────────────────────────────

@dp.message(Command("sysset"))
async def sysset_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        log.warning(f"Permission denied: /sysset by {uid}")
        return await message.reply(roles.OWNER_ONLY)

    args  = (command.args or "").strip().split()
    usage = "⚙️ Usage: /sysset &lt;feature&gt; on|off\n\nFeature keys:\n"
    usage += "\n".join(f"  <code>{k.replace('_enabled','')}</code>" for k in st.FLAG_DEFAULTS)

    if len(args) != 2 or args[1] not in ("on", "off"):
        return await message.reply(usage, parse_mode="HTML")

    feature_key = args[0].strip().lower()
    if not feature_key.endswith("_enabled"):
        feature_key = feature_key + "_enabled"

    if feature_key not in st.FLAG_DEFAULTS:
        return await message.reply(
            f"❌ Unknown feature: <code>{args[0]}</code>\n\n{usage}", parse_mode="HTML"
        )

    enabling     = args[1] == "on"
    user_count   = db.get_total_users()
    feature_stat = st.get_feature_status(feature_key, user_count)

    if enabling and feature_stat["status"] == "locked":
        threshold = feature_stat["threshold"]
        needed    = max(0, threshold - user_count)
        log.warning(f"Admin {uid} tried to enable locked feature '{feature_key}' "
                    f"({user_count}/{threshold} users)")
        return await message.reply(
            f"🔒 <b>{feature_stat['label']}</b> is locked.\n\n"
            f"Required: <b>{threshold:,}</b> users\n"
            f"Current:  <b>{user_count:,}</b> users\n"
            f"Still needed: <b>{needed:,}</b> more users",
            parse_mode="HTML"
        )

    st.set_flag(feature_key, enabling, changed_by=uid)
    state = "✅ Enabled" if enabling else "⬜ Disabled"
    log.info(f"/sysset: '{feature_key}' set to {enabling} by owner {uid}")
    await message.reply(
        f"⚙️ <b>{feature_stat['label']}</b>\n"
        f"Status: {state}",
        parse_mode="HTML"
    )


# ─── Admin commands — Payment account management ─────────────────────────────

@dp.message(Command("addpay"))
async def addpay_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    raw = (command.args or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        return await message.reply(
            "❌ Usage:\n<code>/addpay method | account name | account number | note (optional)</code>\n\n"
            "Example:\n<code>/addpay Wave Pay | Ko Ko | 09123456789 | Wave မှ ပို့ပါ</code>",
            parse_mode="HTML"
        )
    method, name, number = parts[0], parts[1], parts[2]
    note = parts[3] if len(parts) > 3 else ""
    row_id = st.add_payment_account(method, name, number, note)
    if row_id:
        await message.reply(f"✅ Payment account added (ID: {row_id})\n\nMethod: {method}\nName: {name}\nNumber: {number}")
    else:
        await message.reply("❌ Payment account ထည့်မရပါ။")


@dp.message(Command("listpay"))
async def listpay_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    await admin.send_payment_accounts(message)


# ─── /ytcookies — upload YouTube cookies.txt (owner only) ────────────────────

@dp.message(Command("ytcookies"))
async def ytcookies_cmd_handler(message: types.Message, state: FSMContext, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)

    arg = (command.args or "").strip().lower()

    if arg == "status":
        if not os.path.isfile(yt.YT_COOKIES_FILE):
            return await message.reply(
                "🍪 <b>YouTube Cookies Status</b>\n\n"
                "❌ yt_cookies.txt မရှိသေး\n\n"
                "/ytcookies ဖြင့် cookies file upload လုပ်ပါ။",
                parse_mode="HTML",
            )
        stat = os.stat(yt.YT_COOKIES_FILE)
        size_kb = round(stat.st_size / 1024, 1)
        import datetime
        mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        with open(yt.YT_COOKIES_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        cookie_lines = sum(1 for l in lines if l.strip() and not l.startswith("#"))
        has_yt = any(".youtube.com" in l for l in lines)
        valid_mark = "✅" if has_yt else "⚠️"
        return await message.reply(
            f"🍪 <b>YouTube Cookies Status</b>\n\n"
            f"📁 File: <code>yt_cookies.txt</code>\n"
            f"📅 Last updated: <code>{mtime}</code>\n"
            f"📏 Size: <code>{size_kb} KB</code>\n"
            f"🔢 Cookie entries: <code>{cookie_lines}</code>\n"
            f"{valid_mark} YouTube domain: {'ပါသည်' if has_yt else 'မပါ — invalid ဖြစ်နိုင်'}\n\n"
            f"Update လုပ်လိုလျှင် <code>/ytcookies</code> ဖြင့် ဖိုင်ပြန် upload လုပ်ပါ။",
            parse_mode="HTML",
        )

    cookie_status = "✅ ရှိပြီး" if os.path.isfile(yt.YT_COOKIES_FILE) else "❌ မရှိသေး"
    await message.reply(
        f"🍪 <b>YouTube Cookies Setup</b>\n\n"
        f"လက်ရှိ cookies file: {cookie_status}\n\n"
        f"YouTube cookies.txt ဖိုင်ကို document အဖြစ် ပေးပို့ပါ။\n"
        f"(Browser extension: <i>Get cookies.txt LOCALLY</i> သုံး၍ youtube.com cookies export လုပ်ပါ)\n\n"
        f"⚠️ Netscape format ဖိုင်သာ အသုံးပြုနိုင်သည်။\n\n"
        f"Status ကြည့်ရန်: <code>/ytcookies status</code>",
        parse_mode="HTML",
    )
    await state.set_state(YTCookiesFlow.waiting_file)


@dp.message(YTCookiesFlow.waiting_file, F.document)
async def ytcookies_file_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        await state.clear()
        return await message.reply(roles.OWNER_ONLY)

    doc = message.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".txt") or fname.endswith(".cookies")):
        return await message.reply(
            "❌ .txt ဖိုင်သာ လက်ခံသည်။ cookies.txt ဖိုင်ကို ထပ်ပေးပို့ပါ သို့မဟုတ် /cancel နှိပ်ပါ။"
        )

    try:
        file_info = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        content = file_bytes.read().decode("utf-8", errors="replace")

        if "# Netscape HTTP Cookie File" not in content and ".youtube.com" not in content:
            return await message.reply(
                "❌ ဒါ YouTube Netscape cookies file မဟုတ်ပုံပါသည်။\n"
                "youtube.com cookies ပါသည့် ဖိုင်ကို ပေးပို့ပါ။"
            )

        with open(yt.YT_COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(content)

        await state.clear()
        log.info(f"[YT] cookies file updated by owner uid={uid}")
        await message.reply(
            "✅ <b>YouTube cookies ထည့်ပြီးပြီ!</b>\n\n"
            "ယခု YouTube ဒေါင်းလုဒ် ပြန်ကြိုးစားကြည့်ပါ။\n"
            "Cookies သက်တမ်းကုန်လျှင် ဤ command ဖြင့် ပြန် upload လုပ်ပါ။",
            parse_mode="HTML",
        )
    except Exception as e:
        log.error(f"[YT] cookies save error: {e}")
        await state.clear()
        await message.reply(f"❌ ဖိုင် သိမ်းမရပါ: {e}")


@dp.message(YTCookiesFlow.waiting_file)
async def ytcookies_wrong_type(message: types.Message):
    await message.reply("❌ Document ဖိုင်သာ လက်ခံသည်။ cookies.txt ကို <b>ဖိုင် (Document)</b> အဖြစ် ပေးပို့ပါ။", parse_mode="HTML")


@dp.message(Command("togglepay"))
async def togglepay_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    try:
        account_id = int((command.args or "").strip())
    except ValueError:
        return await message.reply("❌ Usage: /togglepay &lt;id&gt;", parse_mode="HTML")
    result = st.toggle_payment_account(account_id)
    if result is None:
        await message.reply(f"❌ ID {account_id} ရှိသော account မတွေ့ပါ။")
    else:
        state = "✅ Active" if result else "⬜ Inactive"
        await message.reply(f"Payment account [{account_id}] → {state}")


@dp.message(Command("delpay"))
async def delpay_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    try:
        account_id = int((command.args or "").strip())
    except ValueError:
        return await message.reply("❌ Usage: /delpay &lt;id&gt;", parse_mode="HTML")
    ok = st.delete_payment_account(account_id)
    if ok:
        await message.reply(f"🗑 Payment account [{account_id}] ဖျက်ပြီးပါပြီ။")
    else:
        await message.reply(f"❌ ID {account_id} ရှိသော account မတွေ့ပါ သို့မဟုတ် ဖျက်မရပါ။")


# ─── Admin commands — Premium plan management ─────────────────────────────────

@dp.message(Command("addplan"))
async def addplan_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    raw = (command.args or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        return await message.reply(
            "❌ Usage:\n<code>/addplan plan name | days | price | currency (optional)</code>\n\n"
            "Example:\n<code>/addplan 1 Day Premium | 1 | 500 | MMK</code>",
            parse_mode="HTML"
        )
    plan_name = parts[0]
    try:
        days  = int(parts[1])
        price = float(parts[2])
    except ValueError:
        return await message.reply("❌ Days နှင့် Price သည် ဂဏန်းဖြစ်ရမည်။")
    currency = parts[3] if len(parts) > 3 else "MMK"
    row_id = st.add_premium_plan(plan_name, days, price, currency)
    if row_id:
        await message.reply(f"✅ Premium plan added (ID: {row_id})\n\nPlan: {plan_name}\n{days} day(s) — {price:,.0f} {currency}")
    else:
        await message.reply("❌ Premium plan ထည့်မရပါ။")


@dp.message(Command("listplan"))
async def listplan_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    await admin.send_premium_plans(message)


@dp.message(Command("toggleplan"))
async def toggleplan_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    try:
        plan_id = int((command.args or "").strip())
    except ValueError:
        return await message.reply("❌ Usage: /toggleplan &lt;id&gt;", parse_mode="HTML")
    result = st.toggle_premium_plan(plan_id)
    if result is None:
        await message.reply(f"❌ ID {plan_id} ရှိသော plan မတွေ့ပါ။")
    else:
        state = "✅ Active" if result else "⬜ Inactive"
        await message.reply(f"Premium plan [{plan_id}] → {state}")


@dp.message(Command("delplan"))
async def delplan_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    try:
        plan_id = int((command.args or "").strip())
    except ValueError:
        return await message.reply("❌ Usage: /delplan &lt;id&gt;", parse_mode="HTML")
    ok = st.delete_premium_plan(plan_id)
    if ok:
        await message.reply(f"🗑 Premium plan [{plan_id}] ဖျက်ပြီးပါပြီ။")
    else:
        await message.reply(f"❌ ID {plan_id} ရှိသော plan မတွေ့ပါ သို့မဟုတ် ဖျက်မရပါ။")


# ─── Admin commands — Monetization controls ───────────────────────────────────

@dp.message(Command("setlimit"))
async def setlimit_handler(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    raw = (command.args or "").strip()
    try:
        new_limit = int(raw)
        if new_limit < 1:
            raise ValueError
    except ValueError:
        return await message.reply(
            "❌ Usage: /setlimit &lt;number&gt;\n\n"
            "Example: <code>/setlimit 5</code>  → 5 free downloads/day",
            parse_mode="HTML"
        )
    st.set_raw("daily_free_limit", str(new_limit))
    log.info(f"/setlimit: daily_free_limit set to {new_limit} by owner {uid}")
    await message.reply(
        f"✅ Daily free download limit set to <b>{new_limit}</b>.\n\n"
        "Enable monetization via <code>/sysset monetization_enabled on</code> "
        "and ad system via <code>/sysset ad_system_enabled on</code> to activate.",
        parse_mode="HTML"
    )


# ─── Admin keyboard — Manage Roles ───────────────────────────────────────────

@dp.message(F.text == "👥 Manage Roles")
async def manage_roles_button_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_MANAGE_ROLES):
        log.warning(f"Permission denied: manage roles for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    log.info(f"Manage Roles button pressed by owner {uid}")
    await admin.send_role_list(message)
    await message.reply(ROLE_HELP, parse_mode="HTML")


# ─── Broadcast command ────────────────────────────────────────────────────────

@dp.message(Command("bc"))
async def bc_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_BROADCAST):
        log.warning(f"Permission denied: /bc for {uid}")
        return await message.reply(roles.DENIED_MSG)
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        return await message.reply("ပို့မည့်စာသားရေးပါ။")
    text  = parts[1]
    users = db.get_all_users()
    log.info(f"Broadcast by {uid} ({roles.get_role(uid)}) — {len(users)} users")
    success, fail = 0, 0
    for u in users:
        try:
            await bot.send_message(u, text)
            success += 1
        except Exception as e:
            log.warning(f"Broadcast failed for user {u}: {e}")
            fail += 1
    log.info(f"Broadcast complete — success: {success}, failed: {fail}")
    await message.reply(
        f"✅ Broadcast ပို့ပြီးပါပြီ။\n✔ {success} ယောက် ရောက်သည်၊ ✘ {fail} ယောက် မရောက်ပါ။"
    )


# ─── Ban / Unban commands ─────────────────────────────────────────────────────

@dp.message(Command("ban"))
async def ban_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_BAN):
        log.warning(f"Permission denied: /ban for {uid}")
        return await message.reply(roles.DENIED_MSG)
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("ID ထည့်ပေးပါ။ (ဥပမာ - /ban 12345678)")
    target_id = parts[1]
    try:
        db.ban_user(target_id)
        log.info(f"User {target_id} banned by {uid} ({roles.get_role(uid)})")
        await message.reply(f"🚫 User {target_id} ကို Ban လိုက်ပါပြီ။")
    except Exception as e:
        log.error(f"Ban action failed for {target_id}: {e}")
        await message.reply("❌ Ban လုပ်ရာတွင် အမှားဖြစ်သွားပါသည်။")


@dp.message(Command("unban"))
async def unban_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_BAN):
        log.warning(f"Permission denied: /unban for {uid}")
        return await message.reply(roles.DENIED_MSG)
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("ID ထည့်ပေးပါ။ (ဥပမာ - /unban 12345678)")
    target_id = parts[1]
    try:
        db.unban_user(target_id)
        log.info(f"User {target_id} unbanned by {uid} ({roles.get_role(uid)})")
        await message.reply(f"🔓 User {target_id} ကို Unban လိုက်ပါပြီ။")
    except Exception as e:
        log.error(f"Unban action failed for {target_id}: {e}")
        await message.reply("❌ Unban လုပ်ရာတွင် အမှားဖြစ်သွားပါသည်။")


# ─── Role management commands (Owner only) ───────────────────────────────────

@dp.message(Command("addadmin"))
async def addadmin_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_MANAGE_ROLES):
        log.warning(f"Permission denied: /addadmin for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("ID ထည့်ပေးပါ။ (ဥပမာ - /addadmin 12345678)")
    try:
        target = int(parts[1])
    except ValueError:
        return await message.reply("❌ User ID မှားနေပါသည်။")
    if roles.add_role(target, roles.ADMIN, uid):
        await message.reply(f"✅ User <code>{target}</code> ကို Admin အဖြစ် ထည့်လိုက်ပါပြီ။",
                            parse_mode="HTML")
    else:
        await message.reply("❌ Admin ထည့်ရာတွင် အမှားဖြစ်သွားပါသည်။")


@dp.message(Command("removeadmin"))
async def removeadmin_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_MANAGE_ROLES):
        log.warning(f"Permission denied: /removeadmin for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("ID ထည့်ပေးပါ။ (ဥပမာ - /removeadmin 12345678)")
    try:
        target = int(parts[1])
    except ValueError:
        return await message.reply("❌ User ID မှားနေပါသည်။")
    result = roles.remove_role(target, uid)
    if result == "ok":
        await message.reply(f"✅ User <code>{target}</code> ၏ role ကို ဖျက်လိုက်ပါပြီ။",
                            parse_mode="HTML")
    elif result == "not_found":
        await message.reply("ℹ️ ထိုသူတွင် role မရှိပါ။")
    elif result == "last_owner":
        await message.reply("⛔ တစ်ဦးတည်းသော Owner ကို မဖျက်နိုင်ပါ။")
    else:
        await message.reply("❌ Role ဖျက်ရာတွင် အမှားဖြစ်သွားပါသည်။")


@dp.message(Command("addsupport"))
async def addsupport_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_MANAGE_ROLES):
        log.warning(f"Permission denied: /addsupport for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("ID ထည့်ပေးပါ။ (ဥပမာ - /addsupport 12345678)")
    try:
        target = int(parts[1])
    except ValueError:
        return await message.reply("❌ User ID မှားနေပါသည်။")
    if roles.add_role(target, roles.SUPPORT, uid):
        await message.reply(f"✅ User <code>{target}</code> ကို Support အဖြစ် ထည့်လိုက်ပါပြီ။",
                            parse_mode="HTML")
    else:
        await message.reply("❌ Support ထည့်ရာတွင် အမှားဖြစ်သွားပါသည်။")


@dp.message(Command("removesupport"))
async def removesupport_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_MANAGE_ROLES):
        log.warning(f"Permission denied: /removesupport for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("ID ထည့်ပေးပါ။ (ဥပမာ - /removesupport 12345678)")
    try:
        target = int(parts[1])
    except ValueError:
        return await message.reply("❌ User ID မှားနေပါသည်။")
    result = roles.remove_role(target, uid)
    if result == "ok":
        await message.reply(f"✅ User <code>{target}</code> ၏ role ကို ဖျက်လိုက်ပါပြီ။",
                            parse_mode="HTML")
    elif result == "not_found":
        await message.reply("ℹ️ ထိုသူတွင် role မရှိပါ။")
    elif result == "last_owner":
        await message.reply("⛔ တစ်ဦးတည်းသော Owner ကို မဖျက်နိုင်ပါ။")
    else:
        await message.reply("❌ Role ဖျက်ရာတွင် အမှားဖြစ်သွားပါသည်။")


@dp.message(Command("roles"))
async def roles_list_handler(message: types.Message):
    uid = message.from_user.id
    if not roles.has_permission(uid, roles.PERM_MANAGE_ROLES):
        log.warning(f"Permission denied: /roles for {uid}")
        return await message.reply(roles.OWNER_ONLY)
    log.info(f"Owner {uid} requested role list")
    await admin.send_role_list(message)


# ─── Admin commands — Premium management ─────────────────────────────────────

@dp.message(Command("givepremium"))
async def cmd_givepremium(message: types.Message, command: CommandObject):
    """Grant premium to a user directly. Bypasses all threshold locks.

    Usage: /givepremium <user_id> <days> [reason]
    """
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)

    args = (command.args or "").strip().split(None, 2)
    if len(args) < 2:
        return await message.reply(
            "❌ Usage: <code>/givepremium &lt;user_id&gt; &lt;days&gt; [reason]</code>\n\n"
            "ဥပမာ: <code>/givepremium 123456789 30 VIP Gift</code>",
            parse_mode="HTML",
        )

    try:
        target_id = int(args[0])
        days      = int(args[1])
        if days < 1:
            raise ValueError
    except ValueError:
        return await message.reply(
            "❌ user_id နှင့် days သည် ဂဏန်းဖြစ်ရမည်။ days ≥ 1",
        )

    reason = args[2].strip() if len(args) > 2 else "admin_grant"

    ok = st.grant_premium_admin(target_id, days, reason, granted_by=uid)
    if not ok:
        return await message.reply(f"❌ Premium ပေး၍မရပါ။ Logs စစ်ပါ။")

    # Show resulting expiry
    prem = st.get_premium_status(target_id)
    exp_str = prem["expires_at"].strftime("%Y-%m-%d %H:%M UTC") if prem["expires_at"] else "?"

    log.info(f"/givepremium: user {target_id} → {days}d '{reason}' by owner {uid}")
    await message.reply(
        f"✅ <b>Premium ပေးပြီးပါပြီ</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 User ID: <code>{target_id}</code>\n"
        f"📅 ရက်: <b>{days} ရက်</b>\n"
        f"📋 Plan: {reason}\n"
        f"⏰ Expires: {exp_str}",
        parse_mode="HTML",
    )


@dp.message(Command("revokepremium"))
async def cmd_revokepremium(message: types.Message, command: CommandObject):
    """Remove premium from a user immediately.

    Usage: /revokepremium <user_id>
    """
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)

    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply(
            "❌ Usage: <code>/revokepremium &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )

    target_id = int(raw)

    # Check if they actually have premium
    prem = st.get_premium_status(target_id)
    if not prem["is_premium"]:
        return await message.reply(
            f"ℹ️ User <code>{target_id}</code> တွင် Active premium မရှိပါ။",
            parse_mode="HTML",
        )

    ok = st.revoke_premium(target_id, revoked_by=uid)
    if not ok:
        return await message.reply("❌ Premium ရုပ်သိမ်း၍မရပါ။")

    log.info(f"/revokepremium: user {target_id} revoked by owner {uid}")
    await message.reply(
        f"✅ User <code>{target_id}</code> ၏ Premium ကို ရုပ်သိမ်းပြီးပါပြီ။",
        parse_mode="HTML",
    )


@dp.message(Command("checkuser"))
async def cmd_checkuser(message: types.Message, command: CommandObject):
    """Show full info about a user — premium, ban, downloads, registration.

    Usage: /checkuser <user_id>
    """
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)

    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply(
            "❌ Usage: <code>/checkuser &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )

    target_id = int(raw)
    user_row  = db.get_user(target_id)
    is_banned = db.is_banned(target_id)
    dl_count  = db.get_user_download_count(target_id)
    prem      = st.get_premium_status(target_id)
    role      = roles.get_role(target_id)

    if prem["is_premium"] and prem["expires_at"]:
        prem_line = (
            f"💎 Active | expires {prem['expires_at'].strftime('%Y-%m-%d')}"
            f" ({prem['plan_name'] or 'manual'})"
        )
    else:
        prem_line = "⬜ Free"

    ban_line  = "🚫 Banned" if is_banned else "✅ Not banned"
    role_line = f"🛠 {role}" if role else "👤 Regular user"

    if user_row:
        name     = user_row["first_name"] or "—"
        username = f"@{user_row['username']}" if user_row["username"] else "—"
        joined   = str(user_row["joined_at"])[:10]
    else:
        name = username = joined = "—"

    await message.reply(
        f"👤 <b>User Info</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{target_id}</code>\n"
        f"📛 Name: {name}\n"
        f"🔗 Username: {username}\n"
        f"📅 Joined: {joined}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏷 Role: {role_line}\n"
        f"🚫 Ban: {ban_line}\n"
        f"⭐ Premium: {prem_line}\n"
        f"📥 Downloads: {dl_count}",
        parse_mode="HTML",
    )
    log.info(f"/checkuser: target={target_id} queried by {uid}")


@dp.message(Command("listpremium"))
async def cmd_listpremium(message: types.Message):
    """List all users with active premium."""
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    log.info(f"/listpremium called by owner {uid}")
    await admin.send_active_premium_users(message)


# ─── Image/photo-carousel sender ─────────────────────────────────────────────

_TG_ALBUM_LIMIT  = 10    # Telegram hard limit for send_media_group
_TG_CAPTION_MAX  = 1024  # Telegram hard limit for photo/video captions


def _cap(title: str, prefix: str = "", suffix: str = "") -> str:
    """Build a safe Telegram caption, truncating title so the whole string
    fits within _TG_CAPTION_MAX characters."""
    fixed = prefix + suffix                    # e.g. "🖼 " or "📝 \n📦 1.2 MB"
    limit = _TG_CAPTION_MAX - len(fixed) - 1  # -1 for safety margin
    if limit <= 0:
        return prefix                          # extreme edge case
    if len(title) > limit:
        title = title[:limit - 1] + "…"
    return f"{prefix}{title}{suffix}"


def _image_url(entry) -> str | None:
    """Normalise a tikwm images[] entry — may be a plain string or a dict."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("url") or entry.get("download_url") or entry.get("src")
    return None


async def _send_image_post(uid: int, url: str, data: dict,
                           wait: types.Message, message: types.Message) -> None:
    """Send all images from a TikTok photo carousel post.

    Images are downloaded locally first (using aiohttp with proper Referer /
    User-Agent headers) and then uploaded to Telegram as file objects.  This
    avoids CDN auth / signed-URL expiry failures that occur when Telegram's
    servers try to fetch the CDN URLs directly.

    Telegram limits send_media_group to 2-10 items per album.
    Posts with more than 10 images are split into consecutive batches.

    Single image  → send_photo
    2-10 images   → one send_media_group
    11+ images    → multiple batched send_media_group calls
    """
    raw_images = data.get("images", [])
    image_urls = [u for entry in raw_images if (u := _image_url(entry))]
    title      = data.get("title", "")
    video_id   = data.get("id", "")
    music      = data.get("music", "")

    if not image_urls:
        log.warning(f"Image post for user {uid}: no usable image URLs in response")
        db.log_download(uid, url, "image", "failed", "No images in response")
        await wait.edit_text("❌ Image ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။")
        return

    total = len(image_urls)
    log.info(f"Image post for user {uid}: {total} image(s) — downloading locally...")

    audio_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Audio (MP3) ဒေါင်းမယ်",
                              callback_data=f"aud_{video_id}")]
    ]) if music else None

    temp_paths = []
    try:
        # ── Download all images concurrently to temp files ─────────────────
        temp_paths = await downloader.download_images_to_files(image_urls, uid)
        valid = [(i, p) for i, p in enumerate(temp_paths) if p]

        if not valid:
            log.error(f"All {total} image downloads failed for user {uid}")
            db.log_download(uid, url, "image", "failed", "All image downloads failed")
            await wait.edit_text("❌ Image ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။")
            return

        log.info(f"Downloaded {len(valid)}/{total} image(s) for user {uid}")
        await wait.delete()

        if len(valid) == 1:
            _, path = valid[0]
            caption = _cap(title, "🖼 ") if title else "🖼 TikTok Photo"
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=FSInputFile(path),
                caption=caption,
                reply_markup=audio_kb,
            )

        else:
            # Split into batches of at most _TG_ALBUM_LIMIT
            batches = [valid[i:i + _TG_ALBUM_LIMIT]
                       for i in range(0, len(valid), _TG_ALBUM_LIMIT)]
            for batch_idx, batch in enumerate(batches):
                media_group = [
                    InputMediaPhoto(
                        media=FSInputFile(path),
                        caption=(_cap(title, "🖼 ")
                                 if batch_idx == 0 and i == 0 and title
                                 else None),
                    )
                    for i, (_, path) in enumerate(batch)
                ]
                await bot.send_media_group(chat_id=message.chat.id, media=media_group)
                log.info(f"Sent batch {batch_idx + 1}/{len(batches)} "
                         f"({len(batch)} images) to user {uid}")

            if audio_kb:
                await message.reply("🎵 Audio ဒေါင်းချင်ပါက:", reply_markup=audio_kb)

        db.log_download(uid, url, "image", "success")
        _trigger_referral_validation(uid)
        st.increment_usage(uid)
        log.info(f"All {len(valid)} image(s) sent successfully to user {uid}")

    except Exception as e:
        log.error(f"Image send failed for user {uid}: {e}")
        db.log_download(uid, url, "image", "failed", str(e))
        await message.reply("❌ Image ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။")

    finally:
        downloader.cleanup_files(temp_paths)


async def _send_live_photo_post(uid: int, url: str, data: dict,
                                wait: types.Message, message: types.Message) -> None:
    """Send TikTok Live Photos as short MP4 video clips.

    Each live photo is a still image paired with a short (1-3s) video clip.
    We download the MP4 clips and send them as a video group so the user gets
    the animated version.  Falls back to static images if no clips are found.

    Single clip   → send_video
    2-10 clips    → send_media_group (InputMediaVideo)
    11+ clips     → multiple batched send_media_group calls
    """
    video_urls = downloader.get_live_photo_video_urls(data)
    title      = data.get("title", "")
    video_id   = data.get("id", "")
    music      = data.get("music", "")

    if not video_urls:
        log.warning(f"Live photo post for user {uid}: no video URLs — falling back to images")
        await _send_image_post(uid, url, data, wait, message)
        return

    total = len(video_urls)
    log.info(f"Live photo post for user {uid}: {total} clip(s) — downloading...")

    audio_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Audio (MP3) ဒေါင်းမယ်",
                              callback_data=f"aud_{video_id}")]
    ]) if music else None

    temp_paths = []
    try:
        temp_paths = await downloader.download_live_photos_to_files(video_urls, uid)
        valid = [(i, p) for i, p in enumerate(temp_paths) if p]

        if not valid:
            log.warning(f"All live photo clips failed — falling back to static images for {uid}")
            await _send_image_post(uid, url, data, wait, message)
            return

        log.info(f"Downloaded {len(valid)}/{total} live photo clip(s) for user {uid}")
        await wait.delete()

        if len(valid) == 1:
            _, path = valid[0]
            caption = _cap(title, "📸 ") if title else "📸 TikTok Live Photo"
            await bot.send_video(
                chat_id=message.chat.id,
                video=FSInputFile(path),
                caption=caption,
                reply_markup=audio_kb,
            )
        else:
            batches = [valid[i:i + _TG_ALBUM_LIMIT]
                       for i in range(0, len(valid), _TG_ALBUM_LIMIT)]
            for batch_idx, batch in enumerate(batches):
                media_group = [
                    types.InputMediaVideo(
                        media=FSInputFile(path),
                        caption=(_cap(title, "📸 ")
                                 if batch_idx == 0 and i == 0 and title
                                 else None),
                    )
                    for i, (_, path) in enumerate(batch)
                ]
                await bot.send_media_group(chat_id=message.chat.id, media=media_group)
                log.info(f"Live photo batch {batch_idx + 1}/{len(batches)} "
                         f"({len(batch)} clips) sent to user {uid}")

            if audio_kb:
                await message.reply("🎵 Audio ဒေါင်းချင်ပါက:", reply_markup=audio_kb)

        db.log_download(uid, url, "live_photo", "success")
        _trigger_referral_validation(uid)
        st.increment_usage(uid)
        log.info(f"All {len(valid)} live photo clip(s) sent successfully to user {uid}")

    except Exception as e:
        log.error(f"Live photo send failed for user {uid}: {e}")
        db.log_download(uid, url, "live_photo", "failed", str(e))
        await message.reply("❌ Live Photo ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။")

    finally:
        downloader.cleanup_files(temp_paths)


# ─── TikTok download handler ──────────────────────────────────────────────────

@dp.message(F.text.regexp(r'https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+'))
async def tiktok_handler(message: types.Message):
    uid    = message.from_user.id
    msg_id = message.message_id

    _key = (uid, msg_id)
    if _key in _processing:
        log.warning(f"Duplicate processing blocked for user {uid} message {msg_id}")
        return
    _processing.add(_key)

    try:
        if roles.has_any_role(uid):
            return await message.reply("Admin/Staff သည် Video ဒေါင်းခွင့်မရှိပါ။")
        if db.is_banned(uid):
            log.info(f"Banned user {uid} tried to download")
            return

        # ── Cooldown check ────────────────────────────────────────────────────
        if not cd.should_bypass(uid) and cd.is_on_cooldown(uid):
            secs = cd.remaining(uid)
            log.info(f"Cooldown blocked user {uid} — {secs}s remaining")
            return await message.reply(
                f"⏳ ကျေးဇူးပြု၍ <b>{secs} seconds</b> စောင့်ပြီး ထပ်မံကြိုးစားပါ",
                parse_mode="HTML"
            )

        # ── Daily quota check ──────────────────────────────────────────────────
        remaining = st.get_remaining_quota(uid)
        if remaining == 0:
            limit = st.get_daily_free_limit()
            usage = st.get_user_usage(uid)
            ad_on   = st.get_flag("ad_system_enabled")
            task_on = st.get_flag("task_system_enabled")

            # Build unlock buttons — one row each
            unlock_rows = []
            if ad_on:
                unlock_rows.append([InlineKeyboardButton(
                    text="🎥 Unlock 10 More (Ad)",
                    callback_data="cb_ad_unlock",
                )])
            if task_on:
                unlock_rows.append([InlineKeyboardButton(
                    text="🎯 Complete a Task",
                    callback_data="cb_task_list",
                )])

            body = (
                f"⚠️ <b>Daily limit reached</b> ({limit} downloads/day).\n\n"
                "You've used all your free downloads for today.\n"
            )
            if unlock_rows:
                body += "Tap a button below to unlock more:"
                await message.reply(
                    body, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=unlock_rows),
                )
            else:
                body += "မနက်ဖြန် ထပ်ကြိုးစားပါ။ (Resets every midnight)"
                await message.reply(body, parse_mode="HTML")

            log.info(
                f"Quota blocked user {uid}: "
                f"{usage['daily_used_count']} used, {limit} limit, {usage['extra_quota']} extra"
            )
            return

        url = downloader.extract_url(message.text)
        if not url or not downloader.is_valid_tiktok_url(url):
            log.warning(f"Invalid TikTok URL from user {uid}: {message.text!r}")
            return await message.reply(
                "❌ <b>မမှန်ကန်သော TikTok Link</b>\n\n"
                "Valid TikTok link တစ်ခုကို ပေးပို့ပါ။\n"
                "ℹ️ ကြည့်ရန် → /help",
                parse_mode="HTML",
            )

        # ── Accept request & stamp cooldown ──────────────────────────────────
        cd.set_cooldown(uid)
        log.info(f"Request accepted from user {uid}: {url}")

        wait = await message.reply("⏳ ဒေါင်းလုပ် လုပ်နေသည်... ခဏစောင့်ပါ")

        # ── Fetch metadata ────────────────────────────────────────────────────
        try:
            data = await downloader.fetch_tiktok_data(url)
        except DownloadError as e:
            log.warning(f"Fetch failed for user {uid} — {e}")
            db.log_download(uid, url, "unknown", "failed", str(e))
            await wait.delete()
            return await message.reply(
                f"❌ <b>ဗီဒီယို ရယူမရပါ</b>\n\n{e.user_message()}\n\n"
                "• Private ဗီဒီယို မဟုတ်ကြောင်း သေချာပါ\n"
                "• Link မှန်ကန်ကြောင်း စစ်ဆေးပါ",
                parse_mode="HTML",
            )

        # ── Branch: live_photo / image / video ───────────────────────────────
        content_type = downloader.get_content_type(data)
        log.info(f"Content type detected for user {uid}: {content_type}")

        if content_type == "live_photo":
            if not downloader.check_image_access(uid):
                await wait.edit_text(
                    "🔒 Live Photo download သည် Premium feature ဖြစ်သည်။\n"
                    "မကြာမီ ဖွင့်ပေးမည်ဖြစ်သည်။"
                )
                return
            await _send_live_photo_post(uid, url, data, wait, message)
            return

        if content_type == "image":
            if not downloader.check_image_access(uid):
                await wait.edit_text(
                    "🔒 Image download သည် Premium feature ဖြစ်သည်။\n"
                    "မကြာမီ ဖွင့်ပေးမည်ဖြစ်သည်။"
                )
                return
            await _send_image_post(uid, url, data, wait, message)
            return

        # ── Video flow ────────────────────────────────────────────────────────
        video_url     = data["play"]
        video_size_mb = (data.get("size") or 0) / (1024 * 1024)
        title         = data.get("title", "")
        video_id      = data.get("id", "")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Audio (MP3) ဒေါင်းမယ်",
                                  callback_data=f"aud_{video_id}")]
        ])

        log.info(f"Video found for user {uid} — size: {round(video_size_mb, 2)} MB")

        if video_size_mb > fb.MAX_TG_SIZE_MB:
            log.info(f"File too large ({round(video_size_mb, 2)} MB) — sending link to {uid}")
            db.log_download(uid, url, "video", "success")
            _trigger_referral_validation(uid)
            st.increment_usage(uid)
            await wait.edit_text(
                f"⚠️ <b>ဖိုင်ကြီးသဖြင့် ({round(video_size_mb, 2)} MB) Direct Link ပေးလိုက်ပါသည်</b>\n\n"
                f"🔗 <a href=\"{video_url}\">ဗီဒီယို ဒေါင်းရန် နှိပ်ပါ</a>\n\n"
                f"<i>Browser မှ ဖွင့်ပြီး Save လုပ်နိုင်သည်</i>",
                parse_mode="HTML", reply_markup=_add_miniapp_btn(kb)
            )
            return

        temp_path = os.path.join(downloader.TEMP_DIR, f"{uid}_{msg_id}.mp4")
        try:
            log.info(f"Downloading video for user {uid} → temp file...")
            await downloader.download_to_file(video_url, temp_path)
            video_file = FSInputFile(temp_path, filename="video.mp4")
            await bot.send_video(
                chat_id=message.chat.id,
                video=video_file,
                caption=_cap(title, "📝 ", f"\n📦 {round(video_size_mb, 2)} MB"),
                reply_markup=kb
            )
            await wait.delete()
            db.log_download(uid, url, "video", "success")
            _trigger_referral_validation(uid)
            st.increment_usage(uid)
            log.info(f"Video sent successfully to user {uid}")

        except DownloadError as e:
            log.error(f"Delivery failed for user {uid}: {e}")
            db.log_download(uid, url, "video", "failed", str(e))
            await send_admin_alert(bot, ADMIN_ID,
                f"Video delivery failed\nUser: {uid}\nURL: {url}\nError: {e}")
            await wait.edit_text(
                f"⚠️ <b>Telegram သို့ တိုက်ရိုက် ပို့မရပါ</b>\n\n"
                f"🔗 <a href=\"{video_url}\">ဗီဒီယို ဒေါင်းရန် နှိပ်ပါ</a>",
                parse_mode="HTML", reply_markup=_add_miniapp_btn(kb)
            )
        finally:
            downloader.cleanup_file(temp_path)

    finally:
        _processing.discard(_key)


# ─── Audio callback ───────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("aud_"))
async def audio_callback(call: types.CallbackQuery):
    v_id = call.data.split("_")[1]
    await call.answer("🎵 အသံဖိုင် ပို့ပေးနေပါပြီး...")
    uid  = call.from_user.id
    log.info(f"User {uid} requested audio for video ID: {v_id}")

    audio_url = f"https://www.tiktok.com/video/{v_id}"
    try:
        data  = await downloader.fetch_tiktok_data(audio_url)
        music = data.get("music")
        if not music:
            log.warning(f"No music URL in response for video ID: {v_id}")
            db.log_download(uid, audio_url, "audio", "failed", "No music URL")
            await bot.send_message(call.message.chat.id, "❌ Audio ရှာမတွေ့ပါ။")
            return
        await bot.send_audio(call.message.chat.id, audio=music)
        db.log_download(uid, audio_url, "audio", "success")
        _trigger_referral_validation(uid)
        log.info(f"Audio sent to user {uid} for video {v_id}")

    except DownloadError as e:
        log.error(f"Audio failed for video {v_id}: {e}")
        db.log_download(uid, audio_url, "audio", "failed", str(e))
        await bot.send_message(call.message.chat.id, e.user_message())
    except Exception as e:
        log.error(f"Unexpected audio error for video {v_id}: {e}")
        db.log_download(uid, audio_url, "audio", "failed", str(e))
        await bot.send_message(call.message.chat.id,
                               "❌ Audio ဒေါင်းရာတွင် ပြဿနာဖြစ်သွားပါသည်။")


# ─── Ad unlock callback ───────────────────────────────────────────────────────

@dp.callback_query(F.data == "cb_ad_unlock")
async def cb_ad_unlock_handler(call: types.CallbackQuery):
    uid = call.from_user.id

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    if not st.get_flag("monetization_enabled"):
        return await call.answer("ℹ️ System offline.", show_alert=True)

    if not st.get_flag("ad_system_enabled"):
        return await call.answer("❌ Ad system is not available right now.", show_alert=True)

    usage = st.add_extra_quota(uid, st.AD_UNLOCK_AMOUNT)
    remaining = st.get_remaining_quota(uid)
    limit     = st.get_daily_free_limit()
    used      = usage["daily_used_count"]
    extra     = usage["extra_quota"]

    log.info(f"Ad unlock clicked: user {uid} → +{st.AD_UNLOCK_AMOUNT} quota (remaining={remaining})")

    await call.answer(f"✅ +{st.AD_UNLOCK_AMOUNT} downloads unlocked!", show_alert=True)
    try:
        await call.message.edit_text(
            f"✅ <b>Unlocked!</b> +{st.AD_UNLOCK_AMOUNT} downloads added.\n\n"
            f"📊 Today's Usage:\n"
            f"  Free Used:      {used} / {limit}\n"
            f"  Extra Unlocked: +{extra}\n"
            f"  Remaining:      {remaining}\n\n"
            "Send a TikTok link to download now!",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─── Task system — user-facing callbacks ──────────────────────────────────────

def _task_list_kb(pending: list) -> InlineKeyboardMarkup:
    """Build the task-list inline keyboard.

    Each task gets one row:
      • join_channel  → URL button (t.me/…) + Claim callback
      • visit_link    → URL button (https://…) + Claim callback
      • custom_task   → Details callback + Claim callback
    """
    rows = []
    for t in pending:
        tid    = t["id"]
        ttype  = t["task_type"]
        target = (t["target"] or "").strip()
        reward = t["reward_amount"]

        if ttype == "join_channel":
            if target.startswith("@"):
                url = f"https://t.me/{target[1:]}"
            elif target.startswith("http"):
                url = target
            else:
                url = None
            go = (InlineKeyboardButton(text=f"🔗 Join (+{reward})", url=url)
                  if url else
                  InlineKeyboardButton(text=f"📋 Details", callback_data=f"cb_tdo_{tid}"))
        elif ttype == "visit_link":
            url = target if target.startswith("http") else None
            go = (InlineKeyboardButton(text=f"🔗 Visit (+{reward})", url=url)
                  if url else
                  InlineKeyboardButton(text=f"📋 Details", callback_data=f"cb_tdo_{tid}"))
        else:
            go = InlineKeyboardButton(text=f"📋 Details (+{reward})", callback_data=f"cb_tdo_{tid}")

        claim = InlineKeyboardButton(text="✅ I Completed", callback_data=f"cb_tclaim_{tid}")
        rows.append([go, claim])

    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="cb_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "cb_task_list")
async def cb_task_list_handler(call: types.CallbackQuery):
    uid = call.from_user.id
    await call.answer()

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    if not st.get_flag("task_system_enabled"):
        user_count = db.get_total_users()
        needed     = max(0, 5000 - user_count)
        return await call.message.edit_text(
            "🎯 <b>Task System</b>\n\n"
            f"⏳ Coming soon — unlocks at <b>5,000 users</b>.\n"
            f"Currently: <b>{user_count:,}</b> users "
            f"({needed:,} more needed).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="cb_back")]
            ]),
        )

    pending = tk.get_user_pending_tasks(uid)
    log.info(f"Task list viewed by user {uid}: {len(pending)} pending task(s)")

    if not pending:
        active = tk.get_active_tasks()
        if not active:
            msg = "🎯 <b>Tasks</b>\n\nNo tasks available right now. Check back later!"
        else:
            msg = (
                "🎯 <b>Tasks</b>\n\n"
                "✅ You've completed all available tasks!\n"
                "Check back later for new ones."
            )
        return await call.message.edit_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="cb_back")]
            ]),
        )

    # Build task list text
    lines = ["🎯 <b>Available Tasks</b>\n",
             "Complete a task to unlock extra downloads:\n"]
    for i, t in enumerate(pending, 1):
        desc = t["description"] or t["target"] or ""
        lines.append(
            f"<b>{i}. {t['title']}</b> → +{t['reward_amount']} downloads\n"
            f"   {desc}"
        )
    lines.append("\nTap <b>I Completed</b> after finishing a task.")

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_task_list_kb(pending),
    )


@dp.callback_query(F.data.startswith("cb_tdo_"))
async def cb_task_details_handler(call: types.CallbackQuery):
    """Show detailed instructions for a custom/link task."""
    uid = call.from_user.id
    await call.answer()

    try:
        task_id = int(call.data.split("_")[-1])
    except ValueError:
        return

    task = tk.get_task(task_id)
    if not task or not task["is_active"]:
        return await call.answer("❌ Task not found or inactive.", show_alert=True)

    desc   = task["description"] or "No additional details."
    target = task["target"] or ""

    text = (
        f"📋 <b>{task['title']}</b>\n\n"
        f"{desc}\n\n"
        f"🎁 Reward: <b>+{task['reward_amount']} downloads</b>\n"
    )
    if target:
        text += f"🔗 Target: <code>{target}</code>\n"
    text += "\nWhen done, press <b>✅ I Completed</b>."

    rows = [[InlineKeyboardButton(text="✅ I Completed",
                                  callback_data=f"cb_tclaim_{task_id}")],
            [InlineKeyboardButton(text="⬅️ Back to Tasks",
                                  callback_data="cb_task_list")]]
    await call.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("cb_tclaim_"))
async def cb_task_claim_handler(call: types.CallbackQuery):
    """Handle task completion claim — verify, award, mark done."""
    uid = call.from_user.id

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    try:
        task_id = int(call.data.split("_")[-1])
    except ValueError:
        return await call.answer("❌ Invalid task.", show_alert=True)

    # Duplicate-completion guard (fast path before DB call)
    if tk.has_completed_task(uid, task_id):
        await call.answer("ℹ️ You already completed this task.", show_alert=True)
        return

    task = tk.get_task(task_id)
    if not task or not task["is_active"]:
        return await call.answer("❌ Task not found or inactive.", show_alert=True)

    await call.answer()

    task_type = task["task_type"]
    target    = (task["target"] or "").strip()
    reward    = task["reward_amount"]

    # ── Verification ──────────────────────────────────────────────────────────
    if task_type == "join_channel" and target:
        is_member = await tk.check_channel_membership(bot, uid, target)
        if is_member is False:
            # Confirmed NOT a member
            log.info(f"Task {task_id} claim denied for user {uid}: not a channel member")
            return await call.message.edit_text(
                f"⚠️ <b>Not joined yet</b>\n\n"
                f"Please join <code>{target}</code> first, then tap "
                "<b>✅ I Completed</b> again.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ I Completed",
                                          callback_data=f"cb_tclaim_{task_id}")],
                    [InlineKeyboardButton(text="⬅️ Back to Tasks",
                                          callback_data="cb_task_list")],
                ]),
            )
        # is_member is True or None (bot can't check → trust user)

    # ── Record + reward ───────────────────────────────────────────────────────
    recorded = tk.mark_task_complete(uid, task_id)
    if not recorded:
        # Race condition — completed between the check above and now
        await call.answer("ℹ️ Already completed.", show_alert=True)
        return

    st.add_extra_quota(uid, reward)
    remaining = st.get_remaining_quota(uid)

    log.info(f"Task {task_id} reward given to user {uid}: +{reward} downloads "
             f"(remaining={remaining})")

    # ── Success message ───────────────────────────────────────────────────────
    await call.message.edit_text(
        f"🎉 <b>Task Completed!</b>\n\n"
        f"✅ <b>{task['title']}</b>\n"
        f"🎁 <b>+{reward} downloads</b> added to your quota.\n\n"
        f"📊 Remaining today: <b>{remaining if remaining >= 0 else '∞'}</b>\n\n"
        "Send a TikTok link to download now!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 More Tasks", callback_data="cb_task_list")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="cb_back")],
        ]),
    )


# ─── Task system — admin commands ─────────────────────────────────────────────

@dp.message(Command("addtask"))
async def cmd_addtask(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)

    usage_txt = (
        "❌ Usage:\n"
        "<code>/addtask title|type|target|reward[|description]</code>\n\n"
        "Types: <code>join_channel</code>, <code>visit_link</code>, "
        "<code>custom_task</code>\n\n"
        "Examples:\n"
        "<code>/addtask Join Channel|join_channel|@mychannel|5|Join to get +5</code>\n"
        "<code>/addtask Visit Site|visit_link|https://example.com|3</code>\n"
        "<code>/addtask Share Bot|custom_task|Share with friends|2|Tell 3 friends</code>"
    )

    raw = (command.args or "").strip()
    if not raw:
        return await message.reply(usage_txt, parse_mode="HTML")

    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        return await message.reply(usage_txt, parse_mode="HTML")

    title, task_type, target = parts[0], parts[1], parts[2]
    description = parts[4] if len(parts) >= 5 else ""

    try:
        reward = int(parts[3])
    except ValueError:
        return await message.reply("❌ Reward must be an integer (e.g. 5).")

    if task_type not in tk.TASK_TYPES:
        return await message.reply(
            f"❌ Invalid type <code>{task_type}</code>.\n"
            f"Valid: <code>{'</code>, <code>'.join(tk.TASK_TYPES)}</code>",
            parse_mode="HTML",
        )

    row_id = tk.create_task(title, description, task_type, target, reward, uid)
    if row_id:
        await message.reply(
            f"✅ Task created (ID: <b>{row_id}</b>)\n\n"
            f"Title:   {title}\n"
            f"Type:    <code>{task_type}</code>\n"
            f"Target:  <code>{target}</code>\n"
            f"Reward:  +{reward} downloads\n"
            f"Desc:    {description or '—'}",
            parse_mode="HTML",
        )
    else:
        await message.reply("❌ Task ထည့်မရပါ။")


@dp.message(Command("listtask"))
async def cmd_listtask(message: types.Message):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    rows = tk.get_all_tasks()
    await message.reply(tk.format_tasks_admin_text(rows), parse_mode="HTML")


@dp.message(Command("toggletask"))
async def cmd_toggletask(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)

    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply("❌ Usage: /toggletask &lt;id&gt;", parse_mode="HTML")

    task_id = int(raw)
    state = tk.toggle_task(task_id)
    if state is None:
        await message.reply(f"❌ Task ID {task_id} မတွေ့ပါ။")
    else:
        await message.reply(f"Task [{task_id}] → <b>{state}</b>", parse_mode="HTML")


@dp.message(Command("deltask"))
async def cmd_deltask(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)

    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply("❌ Usage: /deltask &lt;id&gt;", parse_mode="HTML")

    task_id = int(raw)
    ok = tk.delete_task(task_id)
    if ok:
        await message.reply(f"🗑 Task [{task_id}] ဖျက်ပြီးပါပြီ။")
    else:
        await message.reply(f"❌ Task ID {task_id} မတွေ့ပါ သို့မဟုတ် ဖျက်မရပါ။")


@dp.message(Command("taskstats"))
async def cmd_taskstats(message: types.Message):
    uid = message.from_user.id
    if not roles.is_owner(uid):
        return await message.reply(roles.OWNER_ONLY)
    rows = tk.get_task_stats()
    await message.reply(tk.format_task_stats_text(rows), parse_mode="HTML")


# ─── Broadcast system — admin panel button handlers ───────────────────────────

@dp.message(F.text == "📡 Scheduled Jobs")
async def btn_scheduled_jobs(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return
    rows = bc.get_all_broadcasts()
    await message.reply(bc.format_broadcast_list(rows), parse_mode="HTML")


@dp.message(F.text == "📊 Broadcast Stats")
async def btn_broadcast_stats(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return
    stats = bc.get_broadcast_stats()
    await message.reply(bc.format_broadcast_stats(stats), parse_mode="HTML")


@dp.message(F.text == "📢 Create Broadcast")
async def btn_create_broadcast(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return
    await _start_broadcast_flow(message, state)


# ─── Broadcast system — FSM conversation flow ──────────────────────────────────

def _bc_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Text",  callback_data="bctype_text"),
         InlineKeyboardButton(text="🖼 Photo", callback_data="bctype_photo"),
         InlineKeyboardButton(text="🎬 Video", callback_data="bctype_video")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="bctype_cancel")],
    ])


def _bc_timing_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Send Now",  callback_data="bctiming_now")],
        [InlineKeyboardButton(text="🕒 Schedule",  callback_data="bctiming_schedule")],
        [InlineKeyboardButton(text="🔁 Repeat",    callback_data="bctiming_repeat")],
        [InlineKeyboardButton(text="❌ Cancel",     callback_data="bctiming_cancel")],
    ])


def _bc_autodelete_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Yes — Auto Delete", callback_data="bcad_yes"),
         InlineKeyboardButton(text="❌ No",                callback_data="bcad_no")],
    ])


async def _start_broadcast_flow(message: types.Message, state: FSMContext):
    await state.set_state(BroadcastFlow.choosing_type)
    await message.reply(
        "📡 <b>Create Broadcast</b>\n\n"
        "Step 1: Choose broadcast type:",
        parse_mode="HTML",
        reply_markup=_bc_type_kb(),
    )


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    await _start_broadcast_flow(message, state)


@dp.callback_query(BroadcastFlow.choosing_type, F.data.startswith("bctype_"))
async def cb_bc_type(call: types.CallbackQuery, state: FSMContext):
    choice = call.data.split("_")[1]
    await call.answer()

    if choice == "cancel":
        await state.clear()
        return await call.message.edit_text("❌ Broadcast creation cancelled.")

    await state.update_data(bc_type=choice)
    await state.set_state(BroadcastFlow.waiting_content)

    if choice == "text":
        prompt = "Step 2: Send the <b>text message</b> you want to broadcast."
    elif choice == "photo":
        prompt = "Step 2: Send the <b>photo</b> (with optional caption) to broadcast."
    else:
        prompt = "Step 2: Send the <b>video</b> (with optional caption) to broadcast."

    await call.message.edit_text(
        f"📡 <b>Create Broadcast</b>\n\n{prompt}\n\n"
        "Send /cancelflow to cancel.",
        parse_mode="HTML",
    )


@dp.message(BroadcastFlow.waiting_content)
async def fsm_bc_content(message: types.Message, state: FSMContext):
    data    = await state.get_data()
    bc_type = data.get("bc_type", "text")

    content   = ""
    file_id   = None

    if bc_type == "text":
        if not message.text:
            return await message.reply("❌ Please send a text message.")
        content = message.text

    elif bc_type == "photo":
        if not message.photo:
            return await message.reply("❌ Please send a photo.")
        file_id = message.photo[-1].file_id
        content = message.caption or ""

    elif bc_type == "video":
        if not message.video:
            return await message.reply("❌ Please send a video.")
        file_id = message.video.file_id
        content = message.caption or ""

    await state.update_data(bc_content=content, bc_file_id=file_id)
    await state.set_state(BroadcastFlow.previewing)

    # ── Send a live preview so admin sees exactly what users will receive ──
    await message.reply(
        "👁 <b>Preview — Users တွေ အောက်ပါ message ကို မြင်မည်:</b>",
        parse_mode="HTML",
    )
    try:
        if bc_type == "text":
            await message.answer(content)
        elif bc_type == "photo":
            await bot.send_photo(message.chat.id, photo=file_id,
                                 caption=content or None)
        elif bc_type == "video":
            await bot.send_video(message.chat.id, video=file_id,
                                 caption=content or None)
    except Exception as _prev_err:
        log.warning(f"[Broadcast] preview send failed: {_prev_err}")

    prev_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm — Schedule ဆက်လုပ်မည်",
                              callback_data="bcprev_confirm")],
        [InlineKeyboardButton(text="✏️ Edit — ပြန်ပြင်မည်",
                              callback_data="bcprev_redo")],
        [InlineKeyboardButton(text="❌ Cancel",
                              callback_data="bcprev_cancel")],
    ])
    await message.answer(
        "⬆️ <b>ဤ message ကိုပဲ user အားလုံးထံ ပေးပို့မည်</b>\n\n"
        "Confirm မလုပ်မချင်း မပို့ပါ",
        parse_mode="HTML",
        reply_markup=prev_kb,
    )


@dp.callback_query(BroadcastFlow.previewing, F.data.startswith("bcprev_"))
async def cb_bc_preview(call: types.CallbackQuery, state: FSMContext):
    choice = call.data.split("_")[1]
    await call.answer()

    if choice == "cancel":
        await state.clear()
        return await call.message.edit_text("❌ Broadcast creation cancelled.")

    if choice == "redo":
        data    = await state.get_data()
        bc_type = data.get("bc_type", "text")
        if bc_type == "text":
            prompt = "Step 2: Send the <b>text message</b> you want to broadcast."
        elif bc_type == "photo":
            prompt = "Step 2: Send the <b>photo</b> (with optional caption) to broadcast."
        else:
            prompt = "Step 2: Send the <b>video</b> (with optional caption) to broadcast."
        await state.set_state(BroadcastFlow.waiting_content)
        return await call.message.edit_text(
            f"📡 <b>Create Broadcast</b>\n\n{prompt}\n\n"
            "Send /cancelflow to cancel.",
            parse_mode="HTML",
        )

    # choice == "confirm"
    data    = await state.get_data()
    bc_type = data.get("bc_type", "text")
    content = data.get("bc_content", "")
    preview = f'"{content[:60]}{"…" if len(content) > 60 else ""}"' if content else "(no caption)"
    await state.set_state(BroadcastFlow.choosing_timing)
    await call.message.edit_text(
        f"📡 <b>Create Broadcast</b>\n\n"
        f"Type:    <b>{bc_type}</b>\n"
        f"Content: {preview}\n\n"
        "Step 3: Choose when to send:",
        parse_mode="HTML",
        reply_markup=_bc_timing_kb(),
    )


@dp.callback_query(BroadcastFlow.choosing_timing, F.data.startswith("bctiming_"))
async def cb_bc_timing(call: types.CallbackQuery, state: FSMContext):
    choice = call.data.split("_")[1]
    await call.answer()

    if choice == "cancel":
        await state.clear()
        return await call.message.edit_text("❌ Broadcast creation cancelled.")

    if choice == "now":
        await state.update_data(bc_timing="now", bc_schedule=None, bc_interval=None)
        await state.set_state(BroadcastFlow.choosing_autodelete)
        await call.message.edit_text(
            "📡 <b>Create Broadcast</b>\n\n"
            "Step 4: Enable auto-delete?\n\n"
            "Messages will be deleted from each user's chat after a delay.",
            parse_mode="HTML",
            reply_markup=_bc_autodelete_kb(),
        )

    elif choice == "schedule":
        await state.update_data(bc_timing="schedule")
        await state.set_state(BroadcastFlow.waiting_schedule)
        await call.message.edit_text(
            "🕒 <b>Schedule Broadcast</b>\n\n"
            "Enter the time to send (UTC):\n\n"
            "<b>Relative:</b>  <code>30m</code>  <code>2h</code>  <code>1d</code>\n"
            "<b>Absolute:</b>  <code>2026-04-01 14:00</code>\n\n"
            "Send /cancelflow to cancel.",
            parse_mode="HTML",
        )

    elif choice == "repeat":
        await state.update_data(bc_timing="repeat")
        await state.set_state(BroadcastFlow.waiting_interval)
        await call.message.edit_text(
            "🔁 <b>Repeat Broadcast</b>\n\n"
            "Enter repeat interval:\n\n"
            "<code>30s</code>  <code>10m</code>  <code>2h</code>  <code>1d</code>\n\n"
            "First send will happen immediately.\n"
            "Send /cancelflow to cancel.",
            parse_mode="HTML",
        )


@dp.message(BroadcastFlow.waiting_schedule)
async def fsm_bc_schedule(message: types.Message, state: FSMContext):
    from datetime import timezone as _tz
    dt = bc.parse_schedule_time(message.text or "")
    if not dt:
        return await message.reply(
            "❌ Could not parse time. Try:\n"
            "<code>30m</code>  <code>2h</code>  <code>1d</code>  "
            "<code>2026-04-01 14:00</code>",
            parse_mode="HTML",
        )
    from datetime import datetime as _dt
    if dt <= _dt.now(_tz.utc):
        return await message.reply("❌ Schedule time must be in the future.")

    await state.update_data(bc_schedule=dt.isoformat(), bc_interval=None)
    await state.set_state(BroadcastFlow.choosing_autodelete)
    await message.reply(
        "📡 <b>Create Broadcast</b>\n\n"
        "Step 4: Enable auto-delete?\n\n"
        "Messages will be deleted from each user's chat after a delay.",
        parse_mode="HTML",
        reply_markup=_bc_autodelete_kb(),
    )


@dp.message(BroadcastFlow.waiting_interval)
async def fsm_bc_interval(message: types.Message, state: FSMContext):
    secs = bc.parse_interval(message.text or "")
    if not secs or secs < 1:
        return await message.reply(
            "❌ Could not parse interval. Try:\n"
            "<code>30s</code>  <code>5m</code>  <code>2h</code>  <code>1d</code>",
            parse_mode="HTML",
        )
    await state.update_data(bc_interval=secs)
    await state.set_state(BroadcastFlow.choosing_autodelete)
    await message.reply(
        "📡 <b>Create Broadcast</b>\n\n"
        "Step 4: Enable auto-delete?\n\n"
        "Messages will be deleted from each user's chat after a delay.",
        parse_mode="HTML",
        reply_markup=_bc_autodelete_kb(),
    )


@dp.callback_query(BroadcastFlow.choosing_autodelete, F.data.startswith("bcad_"))
async def cb_bc_autodelete(call: types.CallbackQuery, state: FSMContext):
    choice = call.data.split("_")[1]
    await call.answer()

    if choice == "no":
        await state.update_data(bc_autodelete=False, bc_ad_delay=None)
        await call.message.edit_text("✅ Auto-delete: <b>disabled</b>", parse_mode="HTML")
        await _finalize_broadcast(call.message, state)

    elif choice == "yes":
        await state.update_data(bc_autodelete=True)
        await state.set_state(BroadcastFlow.waiting_ad_delay)
        await call.message.edit_text(
            "🗑 <b>Auto-Delete Delay</b>\n\n"
            "How long after sending should messages be deleted?\n\n"
            "<code>10m</code>  <code>1h</code>  <code>24h</code>  <code>2d</code>\n\n"
            "Send /cancelflow to cancel.",
            parse_mode="HTML",
        )


@dp.message(BroadcastFlow.waiting_ad_delay)
async def fsm_bc_ad_delay(message: types.Message, state: FSMContext):
    secs = bc.parse_interval(message.text or "")
    if not secs or secs < 1:
        return await message.reply(
            "❌ Could not parse delay. Try:\n"
            "<code>10m</code>  <code>1h</code>  <code>24h</code>  <code>2d</code>",
            parse_mode="HTML",
        )
    await state.update_data(bc_ad_delay=secs)
    await _finalize_broadcast(message, state)


async def _finalize_broadcast(message: types.Message, state: FSMContext) -> None:
    """Create the broadcast DB row from all accumulated FSM state, then send or schedule."""
    from datetime import datetime, timezone, timedelta

    data       = await state.get_data()
    bc_type    = data.get("bc_type", "text")
    content    = data.get("bc_content", "")
    file_id    = data.get("bc_file_id")
    timing     = data.get("bc_timing", "now")
    interval   = data.get("bc_interval")
    ad_enabled = bool(data.get("bc_autodelete", False))
    ad_delay   = data.get("bc_ad_delay")
    uid        = message.from_user.id

    await state.clear()

    now = datetime.now(timezone.utc)
    if timing == "schedule":
        sched_str = data.get("bc_schedule")
        send_at   = datetime.fromisoformat(sched_str) if sched_str else now
    else:
        send_at = now

    bid = bc.create_broadcast(
        bc_type, content, file_id, send_at, interval, uid,
        auto_delete_enabled=ad_enabled,
        auto_delete_after_seconds=ad_delay if ad_enabled else None,
    )
    if not bid:
        return await message.reply("❌ Broadcast ဖန်တီးမရပါ။")

    label_repeat  = f"Every {bc._interval_label(interval)}" if interval else "Once"
    label_time    = "Immediately" if timing == "now" else send_at.strftime("%Y-%m-%d %H:%M UTC")
    label_ad      = (
        f"🗑 After {bc._interval_label(ad_delay)}"
        if ad_enabled and ad_delay else "❌ Off"
    )

    await message.reply(
        f"✅ <b>Broadcast created!</b> (ID: <b>{bid}</b>)\n\n"
        f"Type:        <b>{bc_type}</b>\n"
        f"Send at:     <b>{label_time}</b>\n"
        f"Repeat:      <b>{label_repeat}</b>\n"
        f"Auto-delete: <b>{label_ad}</b>\n\n"
        "The scheduler will deliver it automatically.",
        parse_mode="HTML",
    )

    # Dispatch immediately when timing is "now" — don't wait for scheduler cycle
    if timing == "now":
        bcast_row = bc.get_broadcast(bid)
        if bcast_row:
            asyncio.create_task(bc.send_broadcast_to_all(bot, dict(bcast_row)))
            log.info(f"Broadcast {bid} dispatched immediately by admin {uid}")


@dp.message(Command("cancelflow"))
async def cmd_cancelflow(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current and "BroadcastFlow" in str(current):
        await state.clear()
        await message.reply("❌ Broadcast creation cancelled.")
    else:
        await message.reply("ℹ️ No active flow to cancel.")


# ─── Broadcast system — management commands ────────────────────────────────────

@dp.message(Command("listbroadcast"))
async def cmd_listbroadcast(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    rows = bc.get_all_broadcasts()
    await message.reply(bc.format_broadcast_list(rows), parse_mode="HTML")


@dp.message(Command("pausebroadcast"))
async def cmd_pausebroadcast(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply("❌ Usage: /pausebroadcast &lt;id&gt;", parse_mode="HTML")
    bid = int(raw)
    ok  = bc.pause_broadcast(bid)
    if ok:
        await message.reply(f"⏸ Broadcast [{bid}] paused.")
        log.info(f"Admin {uid} paused broadcast {bid}")
    else:
        await message.reply(f"❌ Broadcast [{bid}] မတွေ့ပါ သို့မဟုတ် active မဟုတ်ပါ။")


@dp.message(Command("resumebroadcast"))
async def cmd_resumebroadcast(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply("❌ Usage: /resumebroadcast &lt;id&gt;", parse_mode="HTML")
    bid = int(raw)
    ok  = bc.resume_broadcast(bid)
    if ok:
        await message.reply(f"▶️ Broadcast [{bid}] resumed.")
        log.info(f"Admin {uid} resumed broadcast {bid}")
    else:
        await message.reply(f"❌ Broadcast [{bid}] မတွေ့ပါ သို့မဟုတ် active မဟုတ်ပါ။")


@dp.message(Command("cancelbroadcast"))
async def cmd_cancelbroadcast(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply("❌ Usage: /cancelbroadcast &lt;id&gt;", parse_mode="HTML")
    bid = int(raw)
    ok  = bc.cancel_broadcast(bid)
    if ok:
        await message.reply(f"🗑 Broadcast [{bid}] cancelled (deactivated).")
        log.info(f"Admin {uid} cancelled broadcast {bid}")
    else:
        await message.reply(f"❌ Broadcast [{bid}] မတွေ့ပါ။")


@dp.message(Command("broadcaststats"))
async def cmd_broadcaststats(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    stats = bc.get_broadcast_stats()
    await message.reply(bc.format_broadcast_stats(stats), parse_mode="HTML")


@dp.message(Command("broadcastdeliveries"))
async def cmd_broadcastdeliveries(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        return await message.reply(
            "❌ Usage: /broadcastdeliveries &lt;id&gt;", parse_mode="HTML"
        )
    bid    = int(raw)
    bcast  = bc.get_broadcast(bid)
    stats  = bc.get_delivery_stats(bid)
    log.info(f"Admin {uid} viewed deliveries for broadcast {bid}")
    await message.reply(
        bc.format_delivery_stats(bid, dict(bcast) if bcast else None, stats),
        parse_mode="HTML",
    )


@dp.message(Command("autodeletestats"))
async def cmd_autodeletestats(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return await message.reply(roles.DENIED_MSG)
    stats = bc.get_global_delete_stats()
    log.info(f"Admin {uid} viewed global auto-delete stats")
    await message.reply(bc.format_global_delete_stats(stats), parse_mode="HTML")


@dp.message(F.text == "🗑 Auto Delete Stats")
async def btn_autodeletestats(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return
    stats = bc.get_global_delete_stats()
    log.info(f"Admin {uid} viewed global auto-delete stats (panel)")
    await message.reply(bc.format_global_delete_stats(stats), parse_mode="HTML")


@dp.message(F.text == "📦 Broadcast Deliveries")
async def btn_broadcast_deliveries(message: types.Message):
    uid = message.from_user.id
    if not roles.is_admin_or_above(uid):
        return
    await message.reply(
        "📦 <b>Broadcast Deliveries</b>\n\n"
        "Use /broadcastdeliveries &lt;id&gt; to view delivery stats for a specific broadcast.\n\n"
        "Example: <code>/broadcastdeliveries 1</code>",
        parse_mode="HTML",
    )


# ─── YouTube video handler ───────────────────────────────────────────────────

def _yt_resolution_kb(
    formats: list,
    has_thumb: bool,
    max_height: int = 0,
) -> InlineKeyboardMarkup:
    """Build resolution selection keyboard for YouTube.

    Args:
        formats:    List of YTFormat objects (sorted highest-first).
        has_thumb:  Whether to show the thumbnail-only button.
        max_height: When non-zero, only show formats with height < max_height
                    (used for "try lower resolution" retry flow).
    """
    rows = []
    shown = 0
    for fmt in formats:
        if max_height and fmt.height >= max_height:
            continue
        if shown >= 6:
            break
        shown += 1

        # Build size hint string
        if fmt.size_mb > 0:
            if fmt.size_mb > yt.MAX_TG_SIZE_MB:
                hint = f" (~{fmt.size_mb:.0f} MB ⚠️)"
            else:
                hint = f" (~{fmt.size_mb:.0f} MB)"
        else:
            hint = ""

        rows.append([InlineKeyboardButton(
            text=f"📹 {fmt.label}{hint}",
            callback_data=f"yt_res_{fmt.height}",
        )])

    if not rows:
        rows.append([InlineKeyboardButton(
            text="📹 Best Available",
            callback_data="yt_res_0",
        )])

    if has_thumb and not max_height:
        rows.append([InlineKeyboardButton(
            text="🖼 Thumbnail သာ ဒေါင်းမယ်",
            callback_data="yt_thumb",
        )])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="yt_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text.regexp(
    r'https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+'
))
async def youtube_handler(message: types.Message):
    uid    = message.from_user.id
    msg_id = message.message_id

    _key = (uid, msg_id)
    if _key in _processing:
        log.warning(f"Duplicate YT processing blocked user={uid} msg={msg_id}")
        return
    _processing.add(_key)

    try:
        if roles.has_any_role(uid):
            return await message.reply("Admin/Staff သည် Video ဒေါင်းခွင့်မရှိပါ။")
        if db.is_banned(uid):
            log.info(f"Banned user {uid} tried YT download")
            return

        # ── Cooldown ──────────────────────────────────────────────────────────
        if not cd.should_bypass(uid) and cd.is_on_cooldown(uid):
            secs = cd.remaining(uid)
            log.info(f"Cooldown blocked user {uid} (YT) — {secs}s remaining")
            return await message.reply(
                f"⏳ ကျေးဇူးပြု၍ <b>{secs} seconds</b> စောင့်ပြီး ထပ်မံကြိုးစားပါ",
                parse_mode="HTML",
            )

        # ── YouTube daily quota ───────────────────────────────────────────────
        is_vip = ref.is_premium(uid)
        if not is_vip:
            yt_used       = db.get_yt_daily_count(uid)
            ad_unlocked   = db.get_yt_ad_unlocked(uid)
            max_allowed   = YT_FREE_DAILY + (1 if ad_unlocked else 0)

            if yt_used >= max_allowed:
                ad_on      = st.get_flag("ad_system_enabled")
                prem_on    = st.get_flag("premium_enabled")
                unlock_rows = []

                if ad_on and not ad_unlocked:
                    unlock_rows.append([InlineKeyboardButton(
                        text="🎥 Ad ကြည့်ပြီး 1 ပုဒ် ထပ်ဒေါင်းမယ်",
                        callback_data="cb_yt_ad_unlock",
                    )])
                if prem_on:
                    unlock_rows.append([InlineKeyboardButton(
                        text="⭐ Premium/VIP ဝယ်ရန် (Unlimited)",
                        callback_data="cb_premium",
                    )])

                body = (
                    "⚠️ <b>YouTube Daily Limit ပြည့်သွားပါပြီ</b>\n\n"
                    f"တစ်ရက်ကို YouTube video <b>{YT_FREE_DAILY} ပုဒ်</b> သာ "
                    "Free ဒေါင်းနိုင်ပါသည်။\n"
                    "Unlimited ဒေါင်းလိုပါက <b>Premium/VIP</b> အသုံးပြုပါ။\n\n"
                )
                if unlock_rows:
                    body += "📌 ဒေါင်းလုပ်ဆက်ရန် အောက်မှ ရွေးချယ်ပါ:"
                    return await message.reply(
                        body, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=unlock_rows),
                    )
                else:
                    body += "မနက်ဖြန် ထပ်ကြိုးစားပါ။ (Resets every midnight UTC)"
                    return await message.reply(body, parse_mode="HTML")

        # ── Extract URL ───────────────────────────────────────────────────────
        url = yt.extract_youtube_url(message.text or "")
        if not url:
            return await message.reply("❌ YouTube link မမှန်ကန်ပါ။ ထပ်ကြိုးစားပါ။")

        # ── Fetch video info ──────────────────────────────────────────────────
        wait = await message.reply("🔍 YouTube video info ရယူနေသည်... ခဏစောင့်ပါ")
        log.info(f"[YT] info fetch started — user={uid} url={url}")
        info = await yt.fetch_youtube_info(url)

        if not info.ok:
            log.warning(f"[YT] info fetch failed — user={uid}: {info.error_msg}")
            await wait.delete()
            return await message.reply(
                "❌ <b>YouTube Video ရယူမရပါ</b>\n\n"
                "Link မှားနေသည် / Private / Region-blocked ဖြစ်နိုင်သည်\n"
                "📘 /help နှိပ်ပါ",
                parse_mode="HTML",
            )

        if not info.formats:
            await wait.delete()
            return await message.reply(
                "❌ <b>Download format မတွေ့ပါ</b>\n\n"
                "ဤ video ကို ဒေါင်းလို့မရနိုင်ပါ (Copyright / Restricted).",
                parse_mode="HTML",
            )

        # ── Cache & show resolution picker ────────────────────────────────────
        _yt_pending[uid] = {"url": url, "info": info}

        dur_m, dur_s = divmod(info.duration, 60)
        dur_str = f"{dur_m}:{dur_s:02d}" if info.duration else "—"
        vip_note = " 💎 VIP Quality" if is_vip else ""

        await wait.edit_text(
            f"🎬 <b>{info.title[:80]}</b>\n"
            f"⏱ Duration: {dur_str}{vip_note}\n\n"
            "📥 <b>Resolution ရွေးပါ</b> (သို့မဟုတ် Thumbnail ဒေါင်းပါ):",
            parse_mode="HTML",
            reply_markup=_yt_resolution_kb(info.formats, bool(info.thumbnail)),
        )
        log.info(
            f"[YT] resolution picker shown — user={uid} "
            f"formats={len(info.formats)} title={info.title[:40]}"
        )

    finally:
        _processing.discard(_key)


@dp.callback_query(F.data.startswith("yt_res_"))
async def cb_yt_resolution(call: types.CallbackQuery):
    uid = call.from_user.id
    await call.answer()

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    pending = _yt_pending.get(uid)
    if not pending:
        return await call.message.edit_text(
            "❌ Session expired. YouTube link ကို ထပ်ပေးပို့ပါ။"
        )

    try:
        height = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        return await call.message.edit_text("❌ Resolution မမှန်ကန်ပါ။")

    url  = pending["url"]
    info = pending["info"]

    # ── Re-check quota before download ───────────────────────────────────────
    is_vip = ref.is_premium(uid)
    if not is_vip:
        yt_used     = db.get_yt_daily_count(uid)
        ad_unlocked = db.get_yt_ad_unlocked(uid)
        max_allowed = YT_FREE_DAILY + (1 if ad_unlocked else 0)
        if yt_used >= max_allowed:
            _yt_pending.pop(uid, None)
            return await call.message.edit_text(
                "⚠️ YouTube daily limit ပြည့်သွားပါပြီ။ မနက်ဖြန် ထပ်ကြိုးစားပါ။"
            )

    # ── Pre-check: if estimated size > 50 MB skip download → direct link ────
    chosen_fmt = next(
        (f for f in info.formats if f.height == height), None
    ) if height else None
    est_mb = chosen_fmt.size_mb if chosen_fmt else 0.0

    if est_mb > yt.MAX_TG_SIZE_MB:
        log.info(
            f"[YT] pre-check skip download — user={uid} "
            f"height={height} est={est_mb:.0f}MB"
        )
        _yt_pending.pop(uid, None)
        cd.set_cooldown(uid)
        await call.message.edit_text(
            f"⏳ {height}p CDN link ရယူနေသည်... ခဏစောင့်ပါ",
            parse_mode="HTML",
        )
        stream_url, note = await yt.get_direct_url(url, height)

        has_lower = any(f.height < height for f in info.formats)
        lower_rows = []
        if has_lower:
            lower_rows.append([InlineKeyboardButton(
                text="📉 Resolution နိမ့်ချပြီး ဒေါင်းမည် (50 MB အောက်)",
                callback_data=f"yt_lower_{height}",
            )])
            _yt_pending[uid] = {"url": url, "info": info}
        lower_rows.append([InlineKeyboardButton(
            text="❌ Cancel", callback_data="yt_cancel"
        )])
        lower_kb = InlineKeyboardMarkup(inline_keyboard=lower_rows)

        db.log_download(uid, url, "youtube_video", "success", "direct_link")
        _trigger_referral_validation(uid)
        db.increment_yt_daily(uid)
        st.increment_usage(uid)

        if stream_url:
            await call.message.edit_text(
                f"📦 <b>ခန့်မှန်းဖိုင်ဆိုဒ် ~ {est_mb:.0f} MB</b> — Telegram 50 MB limit ကျော်\n\n"
                f"🔗 <a href=\"{stream_url}\">ဒေါင်းလုပ်လုပ်ရန် ဤနေရာနှိပ်ပါ</a>\n\n"
                "⚠️ <i>CDN link သည် ယာယီဖြစ်သဖြင့် မကြာမီ expire ဖြစ်မည်</i>\n"
                "📱 Browser / Download Manager ဖြင့် Save လုပ်နိုင်သည်",
                parse_mode="HTML",
                reply_markup=_add_miniapp_btn(lower_kb),
                disable_web_page_preview=True,
            )
        else:
            await call.message.edit_text(
                f"⚠️ <b>ဖိုင် ~ {est_mb:.0f} MB ကြီး — Telegram 50 MB limit ကျော်</b>\n\n"
                "CDN link ရယူမရပါ။ Resolution နိမ့်ချ၍ ထပ်ကြိုးစားနိုင်သည်",
                parse_mode="HTML",
                reply_markup=_add_miniapp_btn(lower_kb),
            )
        return

    _yt_pending.pop(uid, None)
    cd.set_cooldown(uid)

    await call.message.edit_text(
        f"⏳ <b>{height}p</b> quality ဒေါင်းနေသည်... ခဏစောင့်ပါ\n"
        f"📝 {info.title[:60]}",
        parse_mode="HTML",
    )
    log.info(f"[YT] download started — user={uid} height={height} url={url}")

    result = await yt.download_youtube_video(
        url, uid, call.message.message_id, height
    )

    try:
        if not result.ok:
            db.log_download(uid, url, "youtube_video", "failed", result.error_type)
            return await call.message.edit_text(
                result.user_msg, parse_mode="HTML"
            )

        if result.size_mb > yt.MAX_TG_SIZE_MB:
            log.warning(
                f"[YT] too large — user={uid} size={result.size_mb:.2f}MB"
            )
            # Delete the oversized temp file immediately
            if result.filepath:
                downloader.cleanup_file(result.filepath)

            # Restore pending so the lower-res callback can access info
            _yt_pending[uid] = {"url": url, "info": info}

            # Try to get a direct CDN URL without re-downloading
            await call.message.edit_text(
                f"⏳ CDN link ရယူနေသည်...", parse_mode="HTML"
            )
            stream_url, _note = await yt.get_direct_url(url, height)

            # Build lower-resolution retry keyboard (heights < chosen one)
            has_lower = any(f.height < height for f in info.formats) if height else False
            lower_rows = []
            if has_lower:
                lower_rows.append([InlineKeyboardButton(
                    text="📉 Resolution နိမ့်ချပြီး ထပ်ဒေါင်းမည်",
                    callback_data=f"yt_lower_{height}",
                )])
            lower_rows.append([InlineKeyboardButton(
                text="❌ Cancel", callback_data="yt_cancel"
            )])
            lower_kb = InlineKeyboardMarkup(inline_keyboard=lower_rows)

            db.log_download(uid, url, "youtube_video", "failed", "too_large")

            if stream_url:
                await call.message.edit_text(
                    f"⚠️ <b>ဖိုင် {result.size_mb:.1f} MB ကြီး — Telegram 50 MB limit ကျော်</b>\n\n"
                    f"🔗 <a href=\"{stream_url}\">ဒေါင်းလုပ်လုပ်ရန် ဤနေရာနှိပ်ပါ</a>\n\n"
                    "⚠️ <i>Link သည် CDN မှ ယာယီဖြစ်သဖြင့် အချိန်နည်းနည်းအတွင်း expire ဖြစ်မည်</i>\n"
                    "📱 Browser သို့မဟုတ် Download Manager ဖြင့် ဒေါင်းနိုင်သည်",
                    parse_mode="HTML",
                    reply_markup=_add_miniapp_btn(lower_kb),
                    disable_web_page_preview=True,
                )
            else:
                await call.message.edit_text(
                    f"⚠️ <b>ဖိုင် {result.size_mb:.1f} MB ကြီး — Telegram 50 MB limit ကျော်</b>\n\n"
                    "CDN link ရယူမရပါ။\n"
                    "Resolution နိမ့်ချ၍ ထပ်ကြိုးစားနိုင်သည်",
                    parse_mode="HTML",
                    reply_markup=_add_miniapp_btn(lower_kb),
                )
            return

        quality_tag = f"\n🎬 {result.format_note}" if result.format_note else ""
        size_tag    = f"\n📦 {result.size_mb:.2f} MB"
        vip_tag     = "\n💎 VIP Quality" if is_vip else ""
        caption     = _cap(
            info.title, "🎬 YouTube\n📝 ",
            f"{quality_tag}{size_tag}{vip_tag}"
        )

        vid_file = FSInputFile(result.filepath, filename="youtube_video.mp4")

        if is_vip:
            await bot.send_document(
                chat_id=call.message.chat.id,
                document=vid_file,
                caption=caption,
            )
            log.info(
                f"[YT] sent as document (VIP) — user={uid} "
                f"size={result.size_mb:.2f}MB"
            )
        else:
            await bot.send_video(
                chat_id=call.message.chat.id,
                video=vid_file,
                caption=caption,
            )
            log.info(
                f"[YT] sent as video (FREE) — user={uid} "
                f"size={result.size_mb:.2f}MB"
            )

        try:
            await call.message.delete()
        except Exception:
            pass

        db.log_download(uid, url, "youtube_video", "success")
        _trigger_referral_validation(uid)
        db.increment_yt_daily(uid)
        st.increment_usage(uid)

    except Exception as exc:
        log.error(f"[YT] send error — user={uid}: {exc}")
        db.log_download(uid, url, "youtube_video", "failed", str(exc))
        try:
            await call.message.edit_text(
                "⚠️ YouTube video ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။"
            )
        except Exception:
            pass

    finally:
        if result and result.ok and result.filepath:
            downloader.cleanup_file(result.filepath)


@dp.callback_query(F.data.startswith("yt_lower_"))
async def cb_yt_lower_res(call: types.CallbackQuery):
    """Show a resolution picker limited to heights smaller than the one that failed."""
    uid = call.from_user.id
    await call.answer()

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    pending = _yt_pending.get(uid)
    if not pending:
        return await call.message.edit_text(
            "❌ Session ကုန်သွားပါပြီ။ YouTube link ကို ထပ်ပေးပို့ပါ။"
        )

    try:
        failed_height = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        failed_height = 9999

    info = pending["info"]
    has_thumb = bool(info.thumbnail)

    # Show only resolutions smaller than the one that just failed
    smaller = [f for f in info.formats if f.height < failed_height]
    if not smaller:
        _yt_pending.pop(uid, None)
        return await call.message.edit_text(
            "❌ ထပ်မံ resolution နိမ့်ချ၍မရပါ။ YouTube မှ တိုက်ရိုက် ဒေါင်းနိုင်သည်"
        )

    kb = _yt_resolution_kb(smaller, has_thumb, max_height=failed_height)
    await call.message.edit_text(
        f"📹 <b>{info.title[:60]}</b>\n\n"
        f"⚠️ {failed_height}p သည် 50 MB ကျော်သဖြင့် ပေးမရပါ\n"
        "Resolution နိမ့်ချ၍ ထပ်ဒေါင်းနိုင်သည်:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    log.info(f"[YT] lower-res picker shown — user={uid} failed_height={failed_height}")


@dp.callback_query(F.data == "yt_thumb")
async def cb_yt_thumbnail(call: types.CallbackQuery):
    uid = call.from_user.id
    await call.answer()

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    pending = _yt_pending.get(uid)
    if not pending:
        return await call.message.edit_text(
            "❌ Session expired. YouTube link ကို ထပ်ပေးပို့ပါ။"
        )

    url  = pending["url"]
    info = pending["info"]
    _yt_pending.pop(uid, None)

    if not info.thumbnail:
        return await call.message.edit_text("❌ Thumbnail URL မတွေ့ပါ။")

    await call.message.edit_text("⏳ Thumbnail ဒေါင်းနေသည်... ခဏစောင့်ပါ")
    log.info(f"[YT] thumbnail download — user={uid}")

    thumb_path = await yt.download_thumbnail_from_url(
        info.thumbnail, uid, call.message.message_id
    )

    if not thumb_path:
        return await call.message.edit_text(
            "❌ Thumbnail ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။"
        )

    try:
        caption = _cap(info.title, "🖼 YouTube Thumbnail\n📝 ")
        await bot.send_photo(
            chat_id=call.message.chat.id,
            photo=FSInputFile(thumb_path),
            caption=caption,
        )
        try:
            await call.message.delete()
        except Exception:
            pass
        log.info(f"[YT] thumbnail sent — user={uid}")
    except Exception as e:
        log.error(f"[YT] thumbnail send error — user={uid}: {e}")
        await call.message.edit_text("❌ Thumbnail ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။")
    finally:
        downloader.cleanup_file(thumb_path)


@dp.callback_query(F.data == "yt_cancel")
async def cb_yt_cancel(call: types.CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    _yt_pending.pop(uid, None)
    try:
        await call.message.edit_text("❌ YouTube download cancelled.")
    except Exception:
        pass
    log.info(f"[YT] download cancelled by user={uid}")


@dp.callback_query(F.data == "cb_yt_ad_unlock")
async def cb_yt_ad_unlock(call: types.CallbackQuery):
    """Grant 1 extra YouTube download today after user views an ad."""
    uid = call.from_user.id
    await call.answer()

    if db.is_banned(uid):
        return await call.answer("⛔ You are banned.", show_alert=True)

    if not st.get_flag("ad_system_enabled"):
        return await call.answer("Ad system မဖွင့်ရသေးပါ။", show_alert=True)

    if db.get_yt_ad_unlocked(uid):
        return await call.message.edit_text(
            "ℹ️ ယနေ့ Ad-unlock ကို အသုံးပြုပြီးပါပြီ။\n"
            "မနက်ဖြန် ထပ်ကြိုးစားပါ သို့မဟုတ် Premium/VIP ဝယ်ပါ။"
        )

    db.grant_yt_ad_unlock(uid)
    log.info(f"[YT] ad unlock granted — user={uid}")

    await call.message.edit_text(
        "✅ <b>Ad ကြည့်ပြီးပါပြီ — 1 ပုဒ် ထပ်ဒေါင်းနိုင်ပါပြီ!</b>\n\n"
        "🎬 YouTube link ကို ထပ်ပေးပို့ပါ...",
        parse_mode="HTML",
    )


# ─── Facebook video handler ───────────────────────────────────────────────────

@dp.message(F.text.regexp(
    r'https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch)/\S+',
))
async def facebook_handler(message: types.Message):
    uid    = message.from_user.id
    msg_id = message.message_id

    _key = (uid, msg_id)
    if _key in _processing:
        log.warning(f"Duplicate FB processing blocked user={uid} msg={msg_id}")
        return
    _processing.add(_key)

    try:
        if roles.has_any_role(uid):
            return await message.reply("Admin/Staff သည် Video ဒေါင်းခွင့်မရှိပါ။")
        if db.is_banned(uid):
            log.info(f"Banned user {uid} tried FB download")
            return

        # ── Cooldown ──────────────────────────────────────────────────────────
        if not cd.should_bypass(uid) and cd.is_on_cooldown(uid):
            secs = cd.remaining(uid)
            log.info(f"Cooldown blocked user {uid} (FB) — {secs}s remaining")
            return await message.reply(
                f"⏳ ကျေးဇူးပြု၍ <b>{secs} seconds</b> စောင့်ပြီး ထပ်မံကြိုးစားပါ",
                parse_mode="HTML",
            )

        # ── Daily quota ───────────────────────────────────────────────────────
        remaining = st.get_remaining_quota(uid)
        if remaining == 0:
            limit   = st.get_daily_free_limit()
            usage   = st.get_user_usage(uid)
            ad_on   = st.get_flag("ad_system_enabled")
            task_on = st.get_flag("task_system_enabled")

            unlock_rows = []
            if ad_on:
                unlock_rows.append([InlineKeyboardButton(
                    text="🎥 Unlock 10 More (Ad)", callback_data="cb_ad_unlock",
                )])
            if task_on:
                unlock_rows.append([InlineKeyboardButton(
                    text="🎯 Complete a Task", callback_data="cb_task_list",
                )])

            body = (
                f"⚠️ <b>Daily limit reached</b> ({limit} downloads/day).\n\n"
                "You've used all your free downloads for today.\n"
            )
            if unlock_rows:
                body += "Tap a button below to unlock more:"
                await message.reply(
                    body, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=unlock_rows),
                )
            else:
                body += "မနက်ဖြန် ထပ်ကြိုးစားပါ။ (Resets every midnight)"
                await message.reply(body, parse_mode="HTML")

            log.info(
                f"Quota blocked user {uid} (FB): "
                f"{usage['daily_used_count']} used, {limit} limit"
            )
            return

        # ── Extract & validate URL ────────────────────────────────────────────
        url = fb.extract_facebook_url(message.text)
        if not url:
            return await message.reply(
                "❌ <b>Facebook Video Link မမှန်ကန်ပါ</b>\n\n"
                "Public Facebook video/reel link ပေးပို့ပါ။\n"
                "📘 အသုံးပြုနည်း → /help",
                parse_mode="HTML",
            )

        # ── VIP check — determines quality mode and send method ──────────────
        is_vip = ref.is_premium(uid)
        mode_label = "VIP" if is_vip else "FREE"
        log.info(f"[FB] link received — user={uid} mode={mode_label} url={url}")

        cd.set_cooldown(uid)
        wait_text = (
            "💎 Original Quality ဒေါင်းနေသည်... ခဏစောင့်ပါ"
            if is_vip else
            "⏳ Facebook video ဒေါင်းလုပ် လုပ်နေသည်... ခဏစောင့်ပါ"
        )
        wait = await message.reply(wait_text)

        # ── Provider extraction ───────────────────────────────────────────────
        # download_facebook_video always returns a DownloadResult — never raises.
        result = await fb.download_facebook_video(url, uid, msg_id, vip_mode=is_vip)

        try:
            if not result.ok:
                db.log_download(uid, url, "facebook_video", "failed", result.error_type)
                await wait.edit_text(
                    f"❌ <b>Facebook Video ရယူမရပါ</b>\n\n{result.user_msg}",
                    parse_mode="HTML",
                )
                return

            # ── File-size gate ────────────────────────────────────────────────
            if result.size_mb > fb.MAX_TG_SIZE_MB:
                log.warning(f"[FB] too large — user={uid} size={result.size_mb:.2f}MB — extracting direct URL")
                db.log_download(uid, url, "facebook_video", "success", "direct_link")
                _trigger_referral_validation(uid)
                st.increment_usage(uid)

                direct_url = await fb.get_direct_url(url)
                if direct_url:
                    log.info(f"[FB] direct URL extracted for user={uid}")
                    await wait.edit_text(
                        f"⚠️ <b>ဖိုင်ကြီးနေသဖြင့် ({result.size_mb:.2f} MB) "
                        "Telegram သို့ တိုက်ရိုက်ပို့မရပါ</b>\n\n"
                        f"🔗 <a href=\"{direct_url}\">ဗီဒီယို ဒေါင်းရန် နှိပ်ပါ</a>\n\n"
                        "<i>Browser မှ ဖွင့်ပြီး Save လုပ်နိုင်သည်</i>",
                        parse_mode="HTML",
                    )
                else:
                    log.warning(f"[FB] direct URL extraction failed for user={uid}")
                    await wait.edit_text(
                        f"⚠️ <b>ဖိုင်ကြီးနေသဖြင့် ({result.size_mb:.2f} MB) "
                        "Telegram သို့ တိုက်ရိုက်ပို့မရပါ</b>\n\n"
                        "50 MB ကျော်သော ဗီဒီယိုများ Bot မှ ပို့မရပါ။\n"
                        "Original Facebook page မှ ဒေါင်းနိုင်ပါသည်။",
                        parse_mode="HTML",
                    )
                return

            # ── Caption ───────────────────────────────────────────────────────
            quality_tag = f"\n🎬 {result.format_note}" if result.format_note else ""
            size_tag    = f"\n📦 {result.size_mb:.2f} MB"
            vip_tag     = "\n💎 Original Quality (VIP)" if is_vip else ""
            caption     = _cap(
                result.title, "📘 Facebook\n📝 ",
                f"{quality_tag}{size_tag}{vip_tag}"
            )

            # ── Send: document (VIP, no Telegram compression) or video (free) ─
            ext      = os.path.splitext(result.filepath)[1] or ".mp4"
            filename = f"facebook_video{ext}"

            if is_vip:
                doc_file = FSInputFile(result.filepath, filename=filename)
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=doc_file,
                    caption=caption,
                )
                log.info(
                    f"[FB] sent as document (VIP) — user={uid} "
                    f"format={result.format_note} size={result.size_mb:.2f}MB"
                )
            else:
                vid_file = FSInputFile(result.filepath, filename=filename)
                await bot.send_video(
                    chat_id=message.chat.id,
                    video=vid_file,
                    caption=caption,
                )
                log.info(
                    f"[FB] sent as video (FREE) — user={uid} "
                    f"format={result.format_note} size={result.size_mb:.2f}MB"
                )

            await wait.delete()
            db.log_download(uid, url, "facebook_video", "success")
            _trigger_referral_validation(uid)
            st.increment_usage(uid)

            # Optional upgrade nudge for free users
            if not is_vip:
                try:
                    premium_enabled = st.get_flag("premium_enabled")
                    if premium_enabled:
                        await message.reply(
                            "💎 <b>Original quality + Telegram compression မပါ</b> = VIP သာ\n"
                            "/premium နှိပ်၍ Upgrade လုပ်နိုင်သည်",
                            parse_mode="HTML",
                        )
                except Exception:
                    pass

        except Exception as exc:
            log.error(f"[FB] send error — user={uid}: {exc}")
            db.log_download(uid, url, "facebook_video", "failed", str(exc))
            try:
                await wait.edit_text(
                    "⚠️ Facebook video ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။"
                )
            except Exception:
                pass

        finally:
            if result.ok and result.filepath:
                downloader.cleanup_file(result.filepath)

    finally:
        _processing.discard(_key)


# ─── Fallback: non-TikTok, non-Facebook URLs ──────────────────────────────────

@dp.message(F.text.regexp(r'https?://\S+'))
async def non_tiktok_url_handler(message: types.Message):
    uid = message.from_user.id
    if roles.has_any_role(uid) or db.is_banned(uid):
        return
    log.info(f"User {uid} sent unsupported URL")
    await message.reply(
        "❌ <b>ပံ့ပိုးမထားသော Link</b>\n\n"
        "ပံ့ပိုးသော Platform များ:\n"
        "• 🔥 TikTok\n"
        "• 📘 Facebook\n"
        "• 🎬 YouTube\n\n"
        "📘 အသုံးပြုနည်း သိရှိရန် /help နှိပ်ပါ",
        parse_mode="HTML",
    )


# ─── Entry point ──────────────────────────────────────────────────────────────
#
# MODE DETECTION
# • On Replit (dev or deployed): REPLIT_DOMAINS is always set → webhook mode.
#   Each environment registers its own domain as the Telegram webhook URL.
#   This completely eliminates TelegramConflictError from simultaneous polling.
# • Truly local (no REPLIT_DOMAINS): polling mode.
#
_REPLIT_DOMAIN = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
_USE_WEBHOOK   = bool(_REPLIT_DOMAIN)
_IS_DEPLOYED   = os.environ.get("REPLIT_DEPLOYMENT", "0") == "1"


async def _run_webhook(domain: str):
    """Webhook mode — aiohttp server on port 8080, no polling."""
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_path = f"/webhook/{API_TOKEN}"
    webhook_url  = f"https://{domain}{webhook_path}"
    env_label    = "DEPLOYED" if _IS_DEPLOYED else "DEV"

    log.info(f"Starting in WEBHOOK mode [{env_label}] — domain: {domain}")
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    log.info(f"Webhook registered: {webhook_url[:60]}...")

    async def _handle_root(request):
        stats   = db.get_analytics()
        elapsed = int(time.time() - _BOT_START)
        days    = elapsed // 86400
        hours   = (elapsed % 86400) // 3600
        mins    = (elapsed % 3600)  // 60
        secs    = elapsed % 60
        uptime  = f"{days}d {hours:02d}h {mins:02d}m {secs:02d}s"

        total_users = stats.get("total_users",  0)
        total_dl    = stats.get("total_dl",     0)
        success_dl  = stats.get("success_dl",   0)
        failed_dl   = stats.get("failed_dl",    0)
        today_dl    = stats.get("today_dl",     0)
        total_ban   = stats.get("total_banned", 0)
        rate        = round(success_dl / total_dl * 100, 1) if total_dl else 0

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Bot Status Panel</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:32px 16px}}
  h1{{font-size:1.6rem;font-weight:700;margin-bottom:4px;letter-spacing:.5px}}
  .sub{{color:#8b949e;font-size:.85rem;margin-bottom:32px}}
  .badge{{display:inline-flex;align-items:center;gap:6px;background:#1a2e1a;color:#3fb950;border:1px solid #2ea043;border-radius:20px;padding:4px 14px;font-size:.8rem;font-weight:600;margin-bottom:28px}}
  .dot{{width:8px;height:8px;background:#3fb950;border-radius:50%;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;width:100%;max-width:640px;margin-bottom:24px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px 16px;text-align:center}}
  .card .val{{font-size:1.8rem;font-weight:700;color:#58a6ff;line-height:1}}
  .card .lbl{{font-size:.72rem;color:#8b949e;margin-top:6px;text-transform:uppercase;letter-spacing:.6px}}
  .uptime-box{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px 24px;width:100%;max-width:640px;text-align:center;margin-bottom:24px}}
  .uptime-box .val{{font-size:1.1rem;font-weight:600;color:#f0883e;font-family:monospace}}
  .uptime-box .lbl{{font-size:.72rem;color:#8b949e;margin-top:4px;text-transform:uppercase;letter-spacing:.6px}}
  .rate-box{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:14px 24px;width:100%;max-width:640px;margin-bottom:24px}}
  .bar-bg{{background:#21262d;border-radius:8px;height:10px;overflow:hidden;margin:10px 0 4px}}
  .bar-fill{{height:100%;border-radius:8px;background:linear-gradient(90deg,#238636,#3fb950);transition:width .4s}}
  .bar-lbl{{display:flex;justify-content:space-between;font-size:.72rem;color:#8b949e}}
  .footer{{color:#484f58;font-size:.72rem;margin-top:8px}}
</style>
</head>
<body>
<h1>🤖 TikTok &amp; Facebook Downloader Bot</h1>
<p class="sub">Auto-refreshes every 30 seconds</p>
<div class="badge"><span class="dot"></span>ONLINE &amp; RUNNING</div>

<div class="uptime-box">
  <div class="val">{uptime}</div>
  <div class="lbl">Uptime (since last restart)</div>
</div>

<div class="grid">
  <div class="card"><div class="val">{total_users:,}</div><div class="lbl">Total Users</div></div>
  <div class="card"><div class="val">{total_dl:,}</div><div class="lbl">All Downloads</div></div>
  <div class="card"><div class="val">{today_dl:,}</div><div class="lbl">Today</div></div>
  <div class="card"><div class="val">{total_ban:,}</div><div class="lbl">Banned</div></div>
</div>

<div class="rate-box">
  <div class="bar-lbl"><span>✅ Success: {success_dl:,}</span><span>❌ Failed: {failed_dl:,}</span></div>
  <div class="bar-bg"><div class="bar-fill" style="width:{rate}%"></div></div>
  <div class="bar-lbl"><span>Success Rate</span><span>{rate}%</span></div>
</div>

<p class="footer">Powered by Replit VM Deployment &nbsp;•&nbsp; Always-On 24/7</p>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_health(request):
        return web.Response(text="OK")

    # ── Mini App: init module + register routes ───────────────────────────────
    _me = await bot.get_me()
    miniapp.init(bot, API_TOKEN, domain, bot_username=_me.username or "")
    global _MINI_APP_URL
    _MINI_APP_URL = f"https://{domain}/app"

    app = web.Application()
    app.router.add_get("/", _handle_root)
    app.router.add_get("/health", _handle_health)
    miniapp.register_routes(app)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=8080).start()
    log.info("Webhook server listening on :8080")

    # ── Set chat menu button to open the Mini App ─────────────────────────────
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="📱 Open App",
                web_app=WebAppInfo(url=f"https://{domain}/app"),
            )
        )
        log.info(f"[MiniApp] menu button set → https://{domain}/app")
    except Exception as _e:
        log.warning(f"[MiniApp] could not set menu button: {_e}")

    try:
        await bot.set_my_commands([
            BotCommand(command="start",         description="Bot ကို စတင်မည် / ပင်မစာမျက်နှာ"),
            BotCommand(command="help",          description="အသုံးပြုနည်း / ဒေါင်းနည်း"),
            BotCommand(command="myhistory",     description="ကျွန်ုပ်၏ ဒေါင်းမှတ်တမ်း (နောက်ဆုံး ၁၀)"),
            BotCommand(command="quota",         description="ယနေ့ Download Quota စစ်ကြည့်မည်"),
            BotCommand(command="top",           description="🏆 Top Downloaders Leaderboard"),
            BotCommand(command="feedback",      description="💬 Feedback / အကြံပြုချက် ပေးပို့မည်"),
            BotCommand(command="exporthistory", description="📤 Download history CSV export (Admin only)"),
        ])
        log.info("[Bot] Commands menu registered (7 commands)")
    except Exception as _e:
        log.warning(f"[Bot] set_my_commands failed: {_e}")

    try:
        await asyncio.Event().wait()   # run forever
    finally:
        await runner.cleanup()
        await bot.delete_webhook()
        log.info("Webhook server stopped.")


async def _run_polling():
    """Polling mode — only when running fully outside Replit (no public domain)."""
    keep_alive()
    log.info("Starting in POLLING mode (local/no-domain)")
    await dp.start_polling(bot)
    log.info("Bot has stopped.")


async def _referral_cleanup_loop() -> None:
    """Run referral cleanup once per day."""
    while True:
        await asyncio.sleep(86_400)  # 24 hours
        try:
            ref.cleanup_inactive_referrals()
        except Exception as e:
            log.error(f"Referral cleanup task error: {e}")


async def main():
    downloader.cleanup_stale_files()
    db.init_db()
    roles.init_roles(ADMIN_ID)

    asyncio.create_task(_referral_cleanup_loop())
    asyncio.create_task(bc.scheduler_loop(bot))
    asyncio.create_task(bc.deletion_loop(bot))
    ref.cleanup_inactive_referrals()  # run once at startup
    st.init_settings()

    mode = f"WEBHOOK ({'deployed' if _IS_DEPLOYED else 'dev'})" if _USE_WEBHOOK else "POLLING"
    log.info(f"Bot starting in {mode} mode...")
    if _USE_WEBHOOK:
        await _run_webhook(_REPLIT_DOMAIN)
    else:
        await _run_polling()


if __name__ == '__main__':
    asyncio.run(main())
