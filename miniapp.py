"""miniapp.py — Telegram Mini App — Full-featured Video Downloader

Tabs
────
  ⬇️ Download   — TikTok / Facebook / YouTube (video · audio · thumbnail)
  📋 History    — User's last 20 downloads
  👤 Profile    — Quota · Premium status · Stats

Routes
──────
  GET  /app              → Mini App HTML
  POST /api/info         → Video info (title, formats)
  POST /api/dl           → Start download (type: video | audio | thumb)
  POST /api/history      → User download history
  POST /api/profile      → User profile / quota / premium
"""

import asyncio
import glob as _glob
import hashlib
import hmac
import json
import os
import shutil
import time
import urllib.parse

import yt_dlp
from aiohttp import web
from aiogram.types import FSInputFile

import database as db
import downloader
import facebook_downloader as fb
import youtube_downloader as yt
import referral as ref
import settings as st
from logger import log

# ─── Module state ─────────────────────────────────────────────────────────────

_bot          = None
_bot_token    = ""
_domain       = ""
_bot_username = ""
_ffmpeg       = shutil.which("ffmpeg") or ""
_sem: asyncio.Semaphore | None = None

# Direct-download token store  { token_filename -> (filepath, expires_at) }
_dl_tokens: dict[str, tuple[str, float]] = {}
_DL_TOKEN_TTL  = 600   # seconds tokens are valid
_DL_DIRECT_MAX = 100   # MB cap for direct device download; larger → CDN link


def init(bot, bot_token: str, domain: str, bot_username: str = "") -> None:
    global _bot, _bot_token, _domain, _bot_username, _sem
    _bot          = bot
    _bot_token    = bot_token
    _domain       = domain
    _bot_username = bot_username
    _sem          = asyncio.Semaphore(4)
    log.info(f"[MiniApp] initialised — https://{domain}/app")


def register_routes(app: web.Application) -> None:
    app.router.add_get ("/app",            handle_app)
    app.router.add_post("/api/info",       handle_api_info)
    app.router.add_post("/api/dl",         handle_api_dl)
    app.router.add_post("/api/dl-direct",  handle_api_dl_direct)
    app.router.add_post("/api/history",    handle_api_history)
    app.router.add_post("/api/profile",    handle_api_profile)
    app.router.add_get ("/files/{token}",  handle_files)
    log.info("[MiniApp] routes: /app /api/info /api/dl /api/dl-direct /api/history /api/profile /files/{token}")


# ─── initData validation ──────────────────────────────────────────────────────

def _parse_init_data(raw: str) -> dict | None:
    if not raw or not _bot_token:
        return None
    try:
        params: dict[str, str] = {}
        for item in raw.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                params[k] = urllib.parse.unquote_plus(v)
        got = params.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        key   = hmac.new(b"WebAppData", _bot_token.encode(), hashlib.sha256).digest()
        want  = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, got):
            return None
        return json.loads(params.get("user", "{}"))
    except Exception as e:
        log.warning(f"[MiniApp] initData error: {e}")
        return None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ok(**kw):  return web.json_response({"ok": True,  **kw})
def _err(m, s=400): return web.json_response({"ok": False, "error": m}, status=s)

def _c(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


def _user_history(uid: int, limit: int = 20) -> list[dict]:
    try:
        from database import _connect
        with _connect() as conn:
            rows = conn.execute(
                """SELECT media_type, status, created_at
                   FROM downloads WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (uid, limit),
            ).fetchall()
        out = []
        icons = {"video": "📹", "audio": "🎵", "youtube_video": "🎬",
                 "facebook_video": "📘", "direct_link": "🔗"}
        for r in rows:
            mtype  = r["media_type"] or "video"
            status = r["status"] or ""
            ts     = (r["created_at"] or "")[:16].replace("T", " ")
            ok     = status == "success" or "direct" in status
            out.append({"icon": icons.get(mtype, "📥"), "type": mtype,
                        "status": "✅" if ok else "❌", "time": ts})
        return out
    except Exception as e:
        log.warning(f"[MiniApp] history error: {e}")
        return []


# ─── HTML ─────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Video Downloader</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--card:#161b22;--card2:#1c2128;--border:#30363d;
  --text:#e6edf3;--sub:#8b949e;--dim:#484f58;
  --blue:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#f0883e;--purple:#bc8cff;
  --tiktok:#fe2c55;--fb:#1877f2;--yt:#ff0000;
}
html,body{height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);
  display:flex;flex-direction:column;height:100%}

/* ── panels ── */
#panels{flex:1;overflow-y:auto;padding:16px 16px 0}
.panel{display:none}
.panel.active{display:block}

/* ── tab bar ── */
.tabbar{display:flex;background:var(--card);border-top:1px solid var(--border);
  flex-shrink:0;padding-bottom:env(safe-area-inset-bottom,0)}
.tb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:10px 4px 8px;border:none;background:none;color:var(--sub);font-size:.72rem;
  cursor:pointer;gap:3px;transition:color .15s}
.tb .ico{font-size:1.2rem}
.tb.on{color:var(--blue)}

/* ── headings ── */
h1{font-size:1.1rem;font-weight:700;text-align:center;margin-bottom:2px}
.sub{color:var(--sub);font-size:.76rem;text-align:center;margin-bottom:18px}
h2{font-size:.9rem;font-weight:600;color:var(--sub);margin:16px 0 8px;
  text-transform:uppercase;letter-spacing:.5px}

/* ── url input ── */
.url-row{display:flex;gap:8px;margin-bottom:9px}
#url-input{flex:1;background:var(--card);border:1.5px solid var(--border);border-radius:10px;
  padding:12px 14px;color:var(--text);font-size:.95rem;outline:none;
  transition:border-color .2s;min-width:0}
#url-input:focus{border-color:var(--blue)}
#url-input::placeholder{color:var(--dim)}
#clr{background:var(--card);border:1.5px solid var(--border);border-radius:10px;
  padding:0 13px;color:var(--sub);font-size:1rem;cursor:pointer;display:none}
#paste{width:100%;background:var(--card);border:1.5px solid var(--border);border-radius:10px;
  padding:10px;color:var(--blue);font-size:.88rem;cursor:pointer;margin-bottom:14px}
#paste:active{background:#1c2a3d}

/* ── platform card ── */
#pcard{background:var(--card);border:1.5px solid var(--border);border-radius:12px;
  padding:14px;margin-bottom:12px;display:none}
.pbadge{display:inline-flex;align-items:center;gap:5px;border-radius:20px;
  padding:3px 12px;font-size:.75rem;font-weight:600;margin-bottom:9px}
.b-tt{background:#2a0a10;color:var(--tiktok);border:1px solid var(--tiktok)}
.b-fb{background:#0a1529;color:var(--fb);border:1px solid var(--fb)}
.b-yt{background:#2a0a0a;color:var(--yt);border:1px solid var(--yt)}
#vtitle{font-size:.86rem;font-weight:500;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* format picker */
#fmts{display:none;margin-top:12px}
.fl{font-size:.72rem;color:var(--sub);margin-bottom:6px}
.fg{display:flex;flex-wrap:wrap;gap:7px}
.fb{background:#21262d;border:1.5px solid var(--border);border-radius:8px;
  padding:7px 13px;color:var(--text);font-size:.82rem;cursor:pointer;line-height:1.3}
.fb.sel,.fb:active{border-color:var(--blue);background:#1c2a3d;color:var(--blue)}
.warn{color:var(--yellow);font-size:.68rem}

/* ── action grid ── */
#acts{display:none;gap:10px;margin-bottom:12px}
#acts.show{display:grid}
#acts.cols2{grid-template-columns:1fr 1fr}
#acts.cols3{grid-template-columns:1fr 1fr 1fr}
.act{border:none;border-radius:11px;padding:13px 8px;font-size:.88rem;font-weight:600;
  cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:4px;
  transition:opacity .15s}
.act:disabled{opacity:.45;cursor:not-allowed}
.act:not(:disabled):active{opacity:.8}
.act .aico{font-size:1.3rem}
.act-v{background:linear-gradient(135deg,#238636,#2ea043);color:#fff}
.act-a{background:linear-gradient(135deg,#7c3aed,#9d5cf6);color:#fff}
.act-t{background:linear-gradient(135deg,#0e6aa8,#1a85cf);color:#fff}

/* ── info-only button (YouTube) ── */
#infobtn{width:100%;background:var(--card);border:1.5px solid var(--blue);border-radius:12px;
  padding:13px;color:var(--blue);font-size:.92rem;font-weight:600;cursor:pointer;
  margin-bottom:12px;display:none;transition:background .15s}
#infobtn:active{background:#1c2a3d}
#infobtn:disabled{opacity:.5;cursor:not-allowed}

/* ── link card ── */
#lcard{display:none;background:var(--card);border:1.5px solid var(--border);
  border-radius:12px;padding:14px;margin-bottom:12px;text-align:center}
#lcard a{color:var(--blue);font-size:.9rem;font-weight:600;word-break:break-all}
.lnote{color:var(--sub);font-size:.72rem;margin-top:6px;line-height:1.4}

/* ── status bar ── */
#status{font-size:.86rem;padding:11px 14px;border-radius:10px;display:none;
  margin-bottom:12px;line-height:1.5;text-align:center}
.s-proc{background:#0d1f35;color:var(--blue)}
.s-ok  {background:#0d2318;color:var(--green)}
.s-err {background:#2a0d0d;color:var(--red)}
.sp{display:inline-block;width:13px;height:13px;
  border:2px solid rgba(88,166,255,.25);border-top-color:var(--blue);
  border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── history tab ── */
.hlist{display:flex;flex-direction:column;gap:8px;padding-bottom:16px}
.hitem{display:flex;align-items:center;gap:11px;background:var(--card);
  border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.hico{font-size:1.3rem;flex-shrink:0}
.hinfo{flex:1;min-width:0}
.htype{font-size:.82rem;font-weight:600}
.htime{font-size:.72rem;color:var(--sub);margin-top:2px}
.hst{font-size:.9rem;flex-shrink:0}
.empty{text-align:center;color:var(--sub);font-size:.88rem;padding:32px 0}

/* ── profile tab ── */
.pcard{background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:16px;margin-bottom:12px}
.prow{display:flex;justify-content:space-between;align-items:center;
  padding:8px 0;border-bottom:1px solid var(--border)}
.prow:last-child{border-bottom:none}
.plbl{font-size:.82rem;color:var(--sub)}
.pval{font-size:.88rem;font-weight:600}
.avatar{width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,var(--blue),var(--purple));
  display:flex;align-items:center;justify-content:center;font-size:1.5rem;
  margin:0 auto 12px;border:2px solid var(--border)}
.uname{text-align:center;font-size:1rem;font-weight:700;margin-bottom:3px}
.urole{text-align:center;font-size:.76rem;color:var(--sub);margin-bottom:16px}
.bar-bg{background:#21262d;border-radius:6px;height:8px;overflow:hidden;margin-top:6px}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--blue),var(--green));
  transition:width .4s}
.premium-badge{display:inline-flex;align-items:center;gap:5px;
  background:#1a1a2e;color:var(--purple);border:1px solid var(--purple);
  border-radius:20px;padding:3px 12px;font-size:.76rem;font-weight:600}
.free-badge{color:var(--sub);font-size:.8rem}
.loading{text-align:center;color:var(--sub);padding:32px 0;font-size:.88rem}

/* ── mode toggle ── */
.mode-row{display:flex;background:#21262d;border-radius:10px;padding:3px;
  margin-bottom:12px;gap:3px}
.mb{flex:1;border:none;border-radius:8px;padding:9px 4px;font-size:.84rem;
  font-weight:600;cursor:pointer;color:var(--sub);background:transparent;
  transition:all .2s}
.mb.on{background:var(--blue);color:#fff}
.dl-note{font-size:.71rem;color:var(--sub);text-align:center;margin:-6px 0 10px;
  line-height:1.4}

/* ── platform detect pill ── */
.plat-pill{display:inline-flex;align-items:center;gap:4px;border-radius:20px;
  padding:3px 10px;font-size:.73rem;font-weight:600;margin-bottom:8px}
.pp-tt{background:#2a0a10;color:var(--tiktok);border:1px solid var(--tiktok)}
.pp-fb{background:#0a1529;color:var(--fb);border:1px solid var(--fb)}
.pp-yt{background:#2a0a0a;color:var(--yt);border:1px solid var(--yt)}

/* ── retry button ── */
.retry-btn{width:100%;margin-top:8px;background:transparent;
  border:1.5px solid var(--sub);border-radius:8px;padding:8px;
  color:var(--sub);font-size:.82rem;cursor:pointer}
.retry-btn:active{background:#21262d}

/* ── thumbnail preview ── */
.thumb-wrap{border-radius:10px;overflow:hidden;margin-bottom:10px;
  background:#0d1117;max-height:180px}
.thumb-wrap img{width:100%;max-height:180px;object-fit:cover;display:block}

/* ── history filter ── */
.hist-filter{display:flex;gap:6px;margin-bottom:12px;align-items:center}
.hf{flex:1;padding:6px 2px;font-size:.7rem;font-weight:600;border-radius:8px;
  border:1.5px solid var(--border);background:transparent;
  color:var(--sub);cursor:pointer;text-align:center}
.hf.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.hf-ref{padding:6px 10px;border-radius:8px;border:1.5px solid var(--border);
  background:transparent;color:var(--sub);font-size:.8rem;cursor:pointer}
.hf-ref:active{background:#21262d}

/* ── empty state hint ── */
.empty-hint{display:flex;flex-direction:column;align-items:center;
  padding:28px 20px 20px;gap:8px;text-align:center}
.eh-icon{font-size:2.8rem;line-height:1}
.eh-title{font-size:1rem;font-weight:700;color:var(--text)}
.eh-text{font-size:.8rem;color:var(--sub);line-height:1.7}
.eh-tag{background:#161b22;border:1px solid var(--border);border-radius:20px;
  padding:4px 12px;font-size:.74rem;color:var(--blue);margin-top:2px}
.dl-always{background:#0d1117;border:1.5px solid var(--border);border-radius:10px;
  padding:10px 12px;margin-bottom:8px}
.dl-mode-label{font-size:.72rem;color:var(--sub);margin-bottom:6px;text-align:center}

/* ── share button ── */
.share-btn{width:100%;margin-top:10px;background:linear-gradient(135deg,#0e6aa8,#1877f2);
  border:none;border-radius:10px;padding:11px;color:#fff;font-size:.88rem;
  font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;
  gap:6px;transition:opacity .15s}
.share-btn:active{opacity:.8}
.share-bot-btn{width:100%;margin-top:4px;background:var(--card2);border:1.5px solid var(--blue);
  border-radius:10px;padding:11px;color:var(--blue);font-size:.88rem;font-weight:600;
  cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;
  transition:background .15s}
.share-bot-btn:active{background:#1c2a3d}
</style>
</head>
<body>

<div id="panels">

  <!-- ── Download Tab ────────────────────────────────────────────── -->
  <div id="tab-dl" class="panel active">
    <h1>🎬 Video Downloader</h1>
    <p class="sub">TikTok &nbsp;·&nbsp; Facebook &nbsp;·&nbsp; YouTube</p>

    <div class="url-row">
      <input id="url-input" type="url" placeholder="URL ကူးထည့်ပါ…" autocomplete="off"/>
      <button id="clr" onclick="clearUrl()">✕</button>
    </div>
    <button id="paste" onclick="pasteUrl()">📋 Clipboard မှ URL ကူးထည့်ရန်</button>
    <div id="plat-pill" style="display:none"></div>

    <!-- empty state hint -->
    <div id="empty-hint" class="empty-hint">
      <div class="eh-icon">📥</div>
      <div class="eh-title">Video ဒေါင်းဆွဲရန်</div>
      <div class="eh-text">
        TikTok · Facebook · YouTube URL ကို<br>
        အပေါ်မှ ကူးထည့်ပြီး ↓ Download နှိပ်ပါ
      </div>
      <div class="eh-tag">🗂 50MB+ ဖိုင်ကြီးများ · Bot limit မရှိ · တိုက်ရိုက် ဒေါင်း</div>
    </div>

    <!-- always-visible mode selector -->
    <div class="dl-always">
      <div class="dl-mode-label">ဒေါင်းနည်း ရွေးပါ</div>
      <div class="mode-row" id="mode-row">
        <button class="mb on" id="mb-device" onclick="setMode('device')">⬇️ Device ထဲ သိမ်း</button>
        <button class="mb"    id="mb-chat"   onclick="setMode('chat')">📨 Chat ထဲ ပို့</button>
      </div>
      <div class="dl-note" id="dl-note">✅ Device mode — ဖိုင်ကြီးများ (50MB+) ပါ တိုက်ရိုက် ဒေါင်းနိုင်သည်</div>
    </div>

    <div id="pcard">
      <div id="thumb-wrap" class="thumb-wrap" style="display:none"></div>
      <div id="pbadge" class="pbadge"></div>
      <div id="vtitle"></div>
      <div id="fmts">
        <div class="fl">📹 Resolution ရွေးပါ:</div>
        <div class="fg" id="fg"></div>
      </div>
    </div>

    <button id="infobtn" onclick="fetchInfo()">🔍 ဗီဒီယို အချက်အလက် ရယူမည်</button>

    <div id="acts">
      <button id="btn-v" class="act act-v" onclick="doAction('video')">
        <span class="aico">📹</span>Video
      </button>
      <button id="btn-a" class="act act-a" onclick="doAction('audio')" style="display:none">
        <span class="aico">🎵</span>Audio
      </button>
      <button id="btn-t" class="act act-t" onclick="doAction('thumb')" style="display:none">
        <span class="aico">🖼</span>Thumbnail
      </button>
    </div>

    <div id="lcard">
      <div id="lanchor"></div>
      <div class="lnote">⚠️ CDN link ယာယီဖြစ်သဖြင့် မကြာမီ expire ဖြစ်မည်<br>Browser / Download Manager ဖြင့် Save လုပ်ပါ</div>
      <button id="share-link-btn" class="share-btn" onclick="shareLink()" style="display:none">
        📤 Telegram Chat ထဲ Share ရန်
      </button>
    </div>

    <div id="status"></div>
    <button id="retry-btn" class="retry-btn" onclick="retryLast()" style="display:none">🔄 ထပ်ကြိုးစားမည်</button>
  </div>

  <!-- ── History Tab ─────────────────────────────────────────────── -->
  <div id="tab-hist" class="panel">
    <h1>📋 Download History</h1>
    <p class="sub">ဒေါင်းခဲ့သော မှတ်တမ်းများ</p>
    <div class="hist-filter">
      <button class="hf on" id="hf-all" onclick="filterHist('all')">All</button>
      <button class="hf" id="hf-tt" onclick="filterHist('tiktok')">🎵 TT</button>
      <button class="hf" id="hf-fb" onclick="filterHist('facebook')">📘 FB</button>
      <button class="hf" id="hf-yt" onclick="filterHist('youtube')">▶️ YT</button>
      <button class="hf-ref" onclick="refreshHist()">🔄</button>
    </div>
    <div id="hlist" class="hlist"><div class="loading">⏳ ခဏစောင့်ပါ…</div></div>
  </div>

  <!-- ── Profile Tab ─────────────────────────────────────────────── -->
  <div id="tab-profile" class="panel">
    <h1>👤 ကျွန်ုပ်၏ Profile</h1>
    <p class="sub">Status · Quota · Premium</p>
    <div id="profile-content"><div class="loading">⏳ ခဏစောင့်ပါ…</div></div>
    <button class="share-bot-btn" onclick="shareBot()">
      📤 Bot ကို မိတ်ဆွေများထံ Share ရန်
    </button>
  </div>

</div>

<!-- ── Tab Bar ──────────────────────────────────────────────────── -->
<nav class="tabbar">
  <button class="tb on" id="tb-dl"      onclick="switchTab('dl')">
    <span class="ico">⬇️</span><span>Download</span>
  </button>
  <button class="tb"    id="tb-hist"    onclick="switchTab('hist')">
    <span class="ico">📋</span><span>History</span>
  </button>
  <button class="tb"    id="tb-profile" onclick="switchTab('profile')">
    <span class="ico">👤</span><span>Profile</span>
  </button>
</nav>

<script>
/* ── Telegram WebApp bootstrap ── */
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand();
const initData = tg.initData || "";

/* ── State ── */
let platform = null, selH = 0, videoInfo = null;
const inp = document.getElementById("url-input");

/* ── Tab switching ── */
let curTab = "dl";
function switchTab(t) {
  document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".tb").forEach(b => b.classList.remove("on"));
  document.getElementById("tab-"+t).classList.add("active");
  document.getElementById("tb-"+t).classList.add("on");
  curTab = t;
  if (t === "hist")    loadHistory();
  if (t === "profile") loadProfile();
}

/* ── URL input handling ── */
inp.addEventListener("input", onUrl);
function onUrl() {
  const v = inp.value.trim();
  document.getElementById("clr").style.display = v ? "block" : "none";
  hide("pcard"); hide("acts"); hide("lcard"); hide("infobtn"); hide("plat-pill");
  setStatus(""); videoInfo = null; selH = 0;
  if (!v) {
    show("empty-hint");
    try { tg.MainButton.hide(); } catch(e){}
    return;
  }
  hide("empty-hint");
  platform = detect(v);
  if (!platform) {
    setStatus("err", "⚠️ TikTok / Facebook / YouTube URL မဟုတ်ပါ");
    try { tg.MainButton.hide(); } catch(e){}
    return;
  }
  updatePlatPill(platform);
  try {
    tg.MainButton.setText("📥 ဒေါင်းမည်");
    tg.MainButton.show();
  } catch(e){}
  if (platform === "youtube") {
    show("infobtn");
  } else {
    showActions();
  }
}

function detect(u) {
  if (/tiktok\.com|vm\.tiktok|vt\.tiktok/i.test(u)) return "tiktok";
  if (/facebook\.com|fb\.watch/i.test(u))            return "facebook";
  if (/youtube\.com|youtu\.be/i.test(u))             return "youtube";
  return null;
}

function clearUrl() { inp.value = ""; onUrl(); inp.focus(); }

async function pasteUrl() {
  try {
    const t = await navigator.clipboard.readText();
    if (t) { inp.value = t; onUrl(); }
  } catch { inp.focus(); }
}

/* ── Auto-detect URL from clipboard on open ── */
(async function tryAutoPaste() {
  try {
    const t = (await navigator.clipboard.readText() || "").trim();
    if (t && detect(t)) { inp.value = t; onUrl(); }
  } catch(e){}
})();

/* ── Show platform info card ── */
function showPlatCard(d) {
  const cfg = {
    tiktok:   ["b-tt", "🎵 TikTok"],
    facebook: ["b-fb", "📘 Facebook"],
    youtube:  ["b-yt", "▶️ YouTube"],
  };
  const [cls, lbl] = cfg[d.platform] || ["", ""];
  const badge = document.getElementById("pbadge");
  badge.className = "pbadge " + cls;
  badge.textContent = lbl;
  document.getElementById("vtitle").textContent = d.title || "";
  const fmts = document.getElementById("fmts");
  const fg   = document.getElementById("fg");
  if (d.formats && d.formats.length) {
    fg.innerHTML = "";
    d.formats.forEach(f => {
      const btn = document.createElement("button");
      btn.className = "fb";
      const big = f.size_mb > 50;
      const sz  = f.size_mb > 0 ? " (~" + Math.round(f.size_mb) + " MB" + (big ? " ⚠️" : "") + ")" : "";
      btn.innerHTML = f.label + sz + (big ? "<br><span class='warn'>CDN link ပေးမည်</span>" : "");
      btn.dataset.h = f.height;
      btn.onclick = () => {
        document.querySelectorAll(".fb").forEach(b => b.classList.remove("sel"));
        btn.classList.add("sel"); selH = f.height; showActions();
      };
      fg.appendChild(btn);
    });
    fmts.style.display = "block";
  } else {
    fmts.style.display = "none";
  }
  const tw = document.getElementById("thumb-wrap");
  if (tw) {
    if (d.thumbnail) {
      tw.innerHTML = `<img src="${d.thumbnail}" alt="thumbnail"
        onerror="this.parentNode.style.display='none'">`;
      tw.style.display = "block";
    } else {
      tw.style.display = "none";
    }
  }
  show("pcard");
}

/* ── Show action buttons based on platform ── */
function showActions() {
  const acts  = document.getElementById("acts");
  const btnA  = document.getElementById("btn-a");
  const btnT  = document.getElementById("btn-t");

  btnA.style.display = "none";
  btnT.style.display = "none";

  if (platform === "tiktok") {
    btnA.style.display = "";
    acts.className = "act show cols2";
  } else if (platform === "youtube") {
    btnA.style.display = "";
    btnT.style.display = "";
    acts.className = "act show cols3";
  } else {
    acts.className = "act show cols2";
  }
  show("acts");
}

/* ── Fetch YouTube info ── */
async function fetchInfo() {
  const url = inp.value.trim();
  const btn = document.getElementById("infobtn");
  btn.disabled = true; btn.textContent = "⏳ ရယူနေသည်…";
  setStatus("proc", "ဗီဒီယို အချက်အလက် ရယူနေသည်…");
  try {
    const r = await post("/api/info", {url, init_data: initData});
    if (!r.ok) throw new Error(r.error);
    videoInfo = r;
    showPlatCard(r);
    setStatus("");
    /* YouTube: actions appear only after format selected */
    if (platform !== "youtube") showActions();
  } catch (e) {
    setStatus("err", "❌ " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "🔍 ဗီဒီယို အချက်အလက် ရယူမည်";
    hide("infobtn");
  }
}

/* ── Start download ── */
async function doDownload(type) {
  const url = inp.value.trim();
  if (!url) return;
  const btnId = {video:"btn-v", audio:"btn-a", thumb:"btn-t"}[type];
  const btn   = document.getElementById(btnId);
  btn.disabled = true;
  hide("lcard");
  setStatus("proc", '<span class="sp"></span>ဒေါင်းနေသည်… Telegram chat ထဲ ပေးပို့မည်');
  try {
    const r = await post("/api/dl", {url, platform, height: selH, type, init_data: initData});
    if (!r.ok) throw new Error(r.error);
    if (r.link) {
      _sharedLink = r.link;
      document.getElementById("lanchor").innerHTML =
        '<a href="' + r.link + '" target="_blank">⬇️ ဒေါင်းရန် ဤနေရာနှိပ်ပါ</a>';
      document.getElementById("share-link-btn").style.display = "";
      show("lcard");
      setStatus("ok", "✅ CDN link ရရှိပြီ — Browser ဖြင့် Save လုပ်ပါ");
    } else {
      setStatus("ok", "✅ " + (r.message || "Telegram chat ထဲ ပေးပို့ပြီးပါပြီ！"));
    }
  } catch (e) {
    setStatus("err", "❌ " + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ── Download mode (Device vs Chat) ── */
let downloadMode = "device";
function setMode(m) {
  downloadMode = m;
  ["device","chat"].forEach(t => {
    document.getElementById("mb-"+t).classList.toggle("on", t === m);
  });
  document.getElementById("dl-note").textContent = m === "device"
    ? "✅ Device mode — ဖိုင်ကြီးများ (50MB+) ပါ တိုက်ရိုက် ဒေါင်းနိုင်သည်"
    : "📨 Chat mode — Bot မှတဆင့် Telegram ထဲ ပေးပို့မည် (50MB ကန့်သတ်)";
}

/* ── Main Button (Telegram native) download ── */
function mainDownload() {
  const url = inp.value.trim();
  if (!url) {
    try { tg.HapticFeedback.notificationOccurred("error"); } catch(e){}
    setStatus("err", "⚠️ URL ကူးထည့်ပါ");
    return;
  }
  if (!platform) {
    setStatus("err", "⚠️ TikTok / Facebook / YouTube URL မဟုတ်ပါ");
    return;
  }
  if (platform === "youtube") {
    if (!videoInfo) { fetchInfo(); return; }
    doAction("video");
  } else {
    doAction("video");
  }
}
try {
  tg.MainButton.setText("📥 ဒေါင်းမည်");
  tg.MainButton.onClick(mainDownload);
} catch(e){}
async function doAction(type) {
  _lastAction = () => doAction(type);
  if (downloadMode === "device") await doDirectDownload(type);
  else await doDownload(type);
}
async function doDirectDownload(type) {
  const url = inp.value.trim();
  if (!url) return;
  const btnId = {video:"btn-v", audio:"btn-a", thumb:"btn-t"}[type];
  const btn = document.getElementById(btnId);
  btn.disabled = true;
  hide("lcard"); _sharedLink = "";
  setStatus("proc", '<span class="sp"></span>ဒေါင်းနေသည်… ဖိုင်အရွယ်ပေါ် မူတည်၍ 30-120 sec ကြာနိုင်သည်');
  try {
    const r = await post("/api/dl-direct",
      {url, platform, height: selH, type, init_data: initData});
    if (!r.ok) throw new Error(r.error);
    if (r.link) {
      _sharedLink = r.link;
      document.getElementById("lanchor").innerHTML =
        '<a href="' + r.link + '" target="_blank">⬇️ CDN Link — ဒေါင်းရန် နှိပ်ပါ</a>';
      document.getElementById("share-link-btn").style.display = "";
      show("lcard");
      setStatus("ok", "✅ ဖိုင်ကြီးသဖြင့် CDN link ရရှိပြီ — Browser ဖြင့် Save လုပ်ပါ");
    } else if (r.url) {
      tg.openLink(window.location.origin + r.url);
      setStatus("ok", "✅ Browser ဖွင့်ပြီ — Allow / Save နှိပ်ပြီး ဒေါင်းပါ");
    }
  } catch (e) {
    setStatus("err", "❌ " + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ── History ── */
let histLoaded = false, _allHistItems = [], _histFilter = "all";

function filterHist(p) {
  _histFilter = p;
  const map = {all:"hf-all", tiktok:"hf-tt", facebook:"hf-fb", youtube:"hf-yt"};
  document.querySelectorAll(".hf").forEach(b => b.classList.remove("on"));
  const el = document.getElementById(map[p]);
  if (el) el.classList.add("on");
  renderHist();
}

function renderHist() {
  const el = document.getElementById("hlist");
  const items = _histFilter === "all" ? _allHistItems : _allHistItems.filter(i => {
    const t = i.type || "";
    if (_histFilter === "tiktok")   return t === "video" || t === "audio";
    if (_histFilter === "facebook") return t === "facebook_video";
    if (_histFilter === "youtube")  return t === "youtube_video";
    return true;
  });
  if (!items.length) {
    el.innerHTML = '<div class="empty">📭 မှတ်တမ်း မရှိပါ</div>';
    return;
  }
  el.innerHTML = items.map(i =>
    `<div class="hitem">
      <span class="hico">${i.icon}</span>
      <div class="hinfo">
        <div class="htype">${fmtType(i.type)}</div>
        <div class="htime">${i.time}</div>
      </div>
      <span class="hst">${i.status}</span>
    </div>`
  ).join("");
}

function refreshHist() {
  histLoaded = false; _allHistItems = [];
  document.getElementById("hlist").innerHTML =
    '<div class="loading">⏳ ခဏစောင့်ပါ…</div>';
  loadHistory();
}

async function loadHistory() {
  if (histLoaded) return;
  const el = document.getElementById("hlist");
  el.innerHTML = '<div class="loading">⏳ ခဏစောင့်ပါ…</div>';
  try {
    const r = await post("/api/history", {init_data: initData});
    if (!r.ok) throw new Error(r.error);
    if (!r.items.length) {
      el.innerHTML = '<div class="empty">📭 ဒေါင်းမှတ်တမ်း မရှိသေးပါ</div>';
    } else {
      _allHistItems = r.items;
      renderHist();
    }
    histLoaded = true;
  } catch (e) {
    el.innerHTML = '<div class="empty">❌ ဒေါင်းမှတ်တမ်း ရယူမရပါ</div>';
  }
}

function fmtType(t) {
  const m = {video:"📹 TikTok Video", audio:"🎵 TikTok Audio",
    youtube_video:"🎬 YouTube Video", facebook_video:"📘 Facebook Video",
    direct_link:"🔗 Direct Link", "":"📥 Download"};
  return m[t] || (t || "Download");
}

/* ── Profile ── */
let profLoaded = false;
async function loadProfile() {
  if (profLoaded) return;
  const el = document.getElementById("profile-content");
  el.innerHTML = '<div class="loading">⏳ ခဏစောင့်ပါ…</div>';
  try {
    const r = await post("/api/profile", {init_data: initData});
    if (!r.ok) throw new Error(r.error);
    const p = r.profile;
    const pct = p.limit > 0 ? Math.min(100, Math.round(p.used / p.limit * 100)) : 0;
    const premBadge = p.is_premium
      ? `<span class="premium-badge">💎 ${p.plan || "Premium"}</span>`
      : `<span class="free-badge">Free User</span>`;
    el.innerHTML = `
      <div class="pcard">
        <div class="avatar">👤</div>
        <div class="uname">${esc(p.name || "User")}</div>
        <div class="urole">${premBadge}</div>
        <div class="prow"><span class="plbl">User ID</span><span class="pval">${p.uid}</span></div>
        <div class="prow"><span class="plbl">ဒေါင်းပြီးသော စုစုပေါင်း</span><span class="pval">${p.total_dl} ခု</span></div>
        <div class="prow"><span class="plbl">🤝 Referral ပေးပို့မှု</span><span class="pval">${p.ref_count || 0} ဦး</span></div>
        ${p.joined ? `<div class="prow"><span class="plbl">စတင်သည့်နေ့</span><span class="pval">${p.joined}</span></div>` : ""}
      </div>
      ${p.monetization ? `
      <div class="pcard">
        <div class="prow">
          <span class="plbl">ယနေ့ ဒေါင်းမှု</span>
          <span class="pval">${p.used} / ${p.limit > 0 ? p.limit : "∞"}</span>
        </div>
        ${p.limit > 0 ? `<div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div>` : ""}
        <div class="prow"><span class="plbl">YouTube ယနေ့</span><span class="pval">${p.yt_used} ပုဒ်</span></div>
        <div class="prow"><span class="plbl">🔄 Reset</span><span class="pval" id="reset-timer" style="color:var(--yellow)">…</span></div>
      </div>` : ""}
      ${p.is_premium && p.expiry ? `
      <div class="pcard">
        <div class="prow"><span class="plbl">Premium ကုန်ဆုံးသည့်နေ့</span>
          <span class="pval" style="color:var(--purple)">${p.expiry}</span></div>
      </div>` : ""}
    `;
    profLoaded = true;
    (function tick() {
      const t = document.getElementById("reset-timer");
      if (t) { t.textContent = resetTimerStr(); setTimeout(tick, 60000); }
    })();
  } catch (e) {
    el.innerHTML = '<div class="empty">❌ Profile ရယူမရပါ</div>';
  }
}

/* ── Platform pill ── */
function updatePlatPill(p) {
  const el  = document.getElementById("plat-pill");
  const cfg = {
    tiktok:   ["pp-tt", "✓ TikTok URL"],
    facebook: ["pp-fb", "✓ Facebook URL"],
    youtube:  ["pp-yt", "✓ YouTube URL"],
  };
  const entry = cfg[p];
  if (!entry) { el.style.display = "none"; return; }
  el.className = "plat-pill " + entry[0];
  el.textContent = entry[1];
  el.style.display = "inline-flex";
}

/* ── Retry ── */
let _lastAction = null;
function retryLast() {
  if (!_lastAction) return;
  const fn = _lastAction; _lastAction = null;
  document.getElementById("retry-btn").style.display = "none";
  fn();
}

/* ── Quota reset countdown ── */
function resetTimerStr() {
  const now = new Date();
  const mid = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  const d   = mid - now;
  return Math.floor(d / 3600000) + "h " + Math.floor((d % 3600000) / 60000) + "m";
}

/* ── Share ── */
let _sharedLink = "";
function shareLink() {
  if (!_sharedLink) return;
  const text = "🎬 Video download link (ywt-dlp via TikTokDownloaderBot)";
  tg.openTelegramLink(
    "https://t.me/share/url?url=" + encodeURIComponent(_sharedLink) +
    "&text=" + encodeURIComponent(text)
  );
}
function shareBot() {
  const un = "__BOT_USERNAME__";
  tg.openTelegramLink(
    "https://t.me/share/url?url=" + encodeURIComponent("https://t.me/" + un) +
    "&text=" + encodeURIComponent("🎬 TikTok · Facebook · YouTube ဗီဒီယို ဒေါင်းဆွဲနိုင်တဲ့ Bot！")
  );
}

/* ── Utilities ── */
async function post(path, body) {
  const r = await fetch(path, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  return r.json();
}

function setStatus(type, msg) {
  const el    = document.getElementById("status");
  const retry = document.getElementById("retry-btn");
  if (!type || !msg) {
    el.style.display = "none"; el.className = "";
    if (retry) retry.style.display = "none";
    return;
  }
  el.className = "s-" + type;
  el.innerHTML = msg;
  el.style.display = "block";
  if (type === "ok") {
    try { tg.HapticFeedback.notificationOccurred("success"); } catch(e){}
    if (retry) retry.style.display = "none";
  }
  if (type === "err") {
    try { tg.HapticFeedback.notificationOccurred("error"); } catch(e){}
    if (retry && _lastAction) retry.style.display = "";
  }
}

function show(id) { document.getElementById(id).style.display = ""; }
function hide(id) { document.getElementById(id).style.display = "none"; }
function esc(s)   { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
</script>
</body>
</html>
"""


# ─── Route handlers ───────────────────────────────────────────────────────────

async def handle_app(request: web.Request) -> web.Response:
    html = _HTML.replace("__BOT_USERNAME__", _bot_username or "TikTokDownloaderBot")
    return _c(web.Response(text=html, content_type="text/html"))


async def handle_api_info(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    user = _parse_init_data(body.get("init_data", ""))
    if user is None:
        return _err("Telegram auth failed", 401)

    url = (body.get("url") or "").strip()
    if not url:
        return _err("URL required")

    if yt.is_youtube_url(url):
        info = await yt.fetch_youtube_info(url)
        if not info.ok:
            return _err("YouTube info ရယူမရပါ — " + (info.error_msg or ""))
        return _c(_ok(
            platform="youtube",
            title=info.title,
            thumbnail=getattr(info, "thumbnail", ""),
            formats=[{"height": f.height, "label": f.label, "size_mb": f.size_mb}
                     for f in info.formats],
        ))

    if fb.is_facebook_url(url):
        return _c(_ok(platform="facebook", title="Facebook Video", formats=[]))

    if downloader.TIKTOK_PATTERN.search(url):
        try:
            data  = await downloader.fetch_tiktok_data(url)
            title = data.get("title") or data.get("desc") or "TikTok Video"
        except Exception:
            title = "TikTok Video"
        return _c(_ok(platform="tiktok", title=title, formats=[]))

    return _err("TikTok / Facebook / YouTube URL မဟုတ်ပါ")


async def handle_api_dl(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    user = _parse_init_data(body.get("init_data", ""))
    if user is None:
        return _err("Telegram auth failed", 401)

    uid      = user.get("id")
    url      = (body.get("url") or "").strip()
    platform = (body.get("platform") or "").strip()
    height   = int(body.get("height") or 0)
    dl_type  = (body.get("type") or "video").strip()   # video | audio | thumb

    if not uid:
        return _err("User ID missing", 401)
    if db.is_banned(uid):
        return _err("⛔ Bot အသုံးပြုခွင့် ပိတ်ထားသည်")
    if not url:
        return _err("URL required")

    # Auto-detect platform
    if not platform:
        if yt.is_youtube_url(url):         platform = "youtube"
        elif fb.is_facebook_url(url):      platform = "facebook"
        elif downloader.TIKTOK_PATTERN.search(url): platform = "tiktok"
        else: return _err("URL ပံ့ပိုးမထားပါ")

    asyncio.create_task(_dispatch(uid, url, platform, height, dl_type))
    return _c(_ok(message="ဒေါင်းနေသည်… Telegram chat ထဲ မကြာမီ ပေးပို့မည်"))


async def handle_api_history(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    user = _parse_init_data(body.get("init_data", ""))
    if user is None:
        return _err("Telegram auth failed", 401)

    uid = user.get("id")
    if not uid:
        return _err("User ID missing", 401)

    items = await asyncio.get_event_loop().run_in_executor(None, _user_history, uid)
    return _c(_ok(items=items))


async def handle_api_profile(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    user = _parse_init_data(body.get("init_data", ""))
    if user is None:
        return _err("Telegram auth failed", 401)

    uid = user.get("id")
    if not uid:
        return _err("User ID missing", 401)

    def _build():
        row       = db.get_user(uid)
        total_dl  = db.get_user_download_count(uid)
        is_prem   = ref.is_premium(uid)
        expiry    = None
        plan      = None
        monet     = st.get_flag("monetization_enabled")
        used = limit = yt_used = 0

        if is_prem:
            ps     = st.get_premium_status(uid)
            expiry = (ps.get("expiry_date") or "")[:10] if ps else None
            plan   = ps.get("plan_name") if ps else None

        if monet:
            st.reset_usage_if_needed(uid)
            usg    = st.get_user_usage(uid)
            used   = usg.get("daily_used_count", 0)
            limit  = st.get_daily_free_limit()
            yt_used = db.get_yt_daily_count(uid)

        name   = " ".join(filter(None, [
            (row["first_name"] if row else None),
            user.get("first_name"), user.get("last_name"),
        ])) or "User"
        joined = (row["joined_at"][:10] if row and row["joined_at"] else None)

        return {
            "uid": uid, "name": name, "joined": joined,
            "total_dl": total_dl, "is_premium": is_prem,
            "plan": plan, "expiry": expiry,
            "monetization": bool(monet),
            "used": used, "limit": limit if monet else 0, "yt_used": yt_used,
            "ref_count": db.get_referral_count(uid),
        }

    profile = await asyncio.get_event_loop().run_in_executor(None, _build)
    return _c(_ok(profile=profile))


# ─── Token / direct-download helpers ─────────────────────────────────────────

def _purge_tokens() -> None:
    now = time.time()
    dead = [k for k, (fp, exp) in _dl_tokens.items() if now > exp]
    for k in dead:
        fp, _ = _dl_tokens.pop(k)
        downloader.cleanup_file(fp)


async def _download_file(
    uid: int, url: str, platform: str, height: int, dl_type: str
) -> tuple[str | None, str | None]:
    """Download to a temp file. Returns (filepath, None) or (None, cdn_url)."""
    if platform == "tiktok":
        data = await downloader.fetch_tiktok_data(url)
        if dl_type == "audio":
            music = data.get("music")
            if not music:
                raise ValueError("Audio URL ရှာမတွေ့ပါ")
            tmp = os.path.join(downloader.TEMP_DIR,
                               f"dd_tta_{uid}_{int(time.time())}.mp3")
            await downloader.download_to_file(music, tmp)
            return tmp, None
        else:
            vurl   = data.get("play")
            if not vurl:
                raise ValueError("Video URL ရှာမတွေ့ပါ")
            size_mb = (data.get("size") or 0) / (1024 * 1024)
            if size_mb > _DL_DIRECT_MAX:
                return None, vurl
            tmp = os.path.join(downloader.TEMP_DIR,
                               f"dd_tt_{uid}_{int(time.time())}.mp4")
            await downloader.download_to_file(vurl, tmp)
            return tmp, None

    elif platform == "facebook":
        result = await fb.download_facebook_video(url, uid, int(time.time()),
                                                   vip_mode=False)
        if not result.ok:
            raise ValueError(result.user_msg or "Facebook ဒေါင်းမရပါ")
        if result.size_mb and result.size_mb > _DL_DIRECT_MAX:
            if result.filepath:
                downloader.cleanup_file(result.filepath)
            cdn = await fb.get_direct_url(url)
            if cdn:
                return None, cdn
            raise ValueError(f"ဖိုင် {result.size_mb:.0f} MB ကြီး — CDN မရပါ")
        return result.filepath, None

    elif platform == "youtube":
        if dl_type == "thumb":
            info = await yt.fetch_youtube_info(url)
            if not info.ok or not info.thumbnail:
                raise ValueError("Thumbnail ရှာမတွေ့ပါ")
            fp = await yt.download_thumbnail_from_url(
                info.thumbnail, uid, int(time.time()))
            if not fp:
                raise ValueError("Thumbnail ဒေါင်းမရပါ")
            return fp, None

        elif dl_type == "audio":
            tpl = os.path.join(downloader.TEMP_DIR,
                               f"dd_yta_{uid}_{int(time.time())}.%(ext)s")
            fp = await asyncio.get_event_loop().run_in_executor(
                None, _yt_audio_sync, url, tpl)
            if not fp:
                raise ValueError("YouTube Audio ဒေါင်းမရပါ")
            return fp, None

        else:  # video
            info   = await yt.fetch_youtube_info(url)
            chosen = next((f for f in (info.formats or [])
                           if f.height == height), None) if height else None
            est_mb = chosen.size_mb if chosen else 0.0
            if est_mb > _DL_DIRECT_MAX:
                stream_url, _ = await yt.get_direct_url(url, height)
                if stream_url:
                    return None, stream_url
                raise ValueError(f"ဖိုင် ~{est_mb:.0f} MB ကြီး — CDN မရပါ")
            result = await yt.download_youtube_video(
                url, uid, int(time.time()), height)
            if not result.ok:
                raise ValueError(result.user_msg or "YouTube ဒေါင်းမရပါ")
            if result.size_mb > _DL_DIRECT_MAX:
                if result.filepath:
                    downloader.cleanup_file(result.filepath)
                stream_url, _ = await yt.get_direct_url(url, height)
                if stream_url:
                    return None, stream_url
                raise ValueError("ဖိုင်ကြီး — CDN မရပါ")
            return result.filepath, None

    raise ValueError("URL ပံ့ပိုးမထားပါ")


async def handle_api_dl_direct(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    user = _parse_init_data(body.get("init_data", ""))
    if user is None:
        return _err("Telegram auth failed", 401)

    uid = user.get("id")
    if not uid:
        return _err("User ID missing", 401)
    if db.is_banned(uid):
        return _err("⛔ Bot အသုံးပြုခွင့် ပိတ်ထားသည်")

    url      = (body.get("url") or "").strip()
    platform = (body.get("platform") or "").strip()
    height   = int(body.get("height") or 0)
    dl_type  = (body.get("type") or "video").strip()

    if not url:
        return _err("URL required")

    if not platform:
        if yt.is_youtube_url(url):              platform = "youtube"
        elif fb.is_facebook_url(url):           platform = "facebook"
        elif downloader.TIKTOK_PATTERN.search(url): platform = "tiktok"
        else: return _err("URL ပံ့ပိုးမထားပါ")

    _purge_tokens()
    assert _sem is not None
    async with _sem:
        try:
            filepath, cdn_url = await asyncio.wait_for(
                _download_file(uid, url, platform, height, dl_type),
                timeout=180.0,
            )
        except asyncio.TimeoutError:
            return _err("ဒေါင်းချိန် ကုန်ဆုံး — ထပ်ကြိုးစားပါ")
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            log.error(f"[MiniApp] dl-direct uid={uid}: {e}")
            return _err(f"ဒေါင်းမရပါ — {e}")

    if cdn_url:
        db.log_download(uid, url, "direct_link", "success")
        return _c(_ok(link=cdn_url))

    import secrets
    ext   = os.path.splitext(filepath)[1] or ".mp4"
    token = secrets.token_hex(12) + ext
    _dl_tokens[token] = (filepath, time.time() + _DL_TOKEN_TTL)
    db.log_download(uid, url, dl_type, "success")
    return _c(_ok(url=f"/files/{token}"))


async def handle_files(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    entry = _dl_tokens.pop(token, None)
    if not entry:
        raise web.HTTPNotFound(text="File expired or not found")
    filepath, _ = entry
    if not os.path.exists(filepath):
        raise web.HTTPGone(text="File gone")

    ext  = os.path.splitext(token)[1].lstrip(".").lower()
    cmap = {"mp4": "video/mp4", "mp3": "audio/mpeg", "m4a": "audio/mp4",
            "webm": "video/webm", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "opus": "audio/ogg"}
    ctype = cmap.get(ext, "application/octet-stream")
    size  = os.path.getsize(filepath)
    fname = os.path.basename(filepath)

    resp = web.StreamResponse(headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Content-Type":        ctype,
        "Content-Length":      str(size),
        "Cache-Control":       "no-store",
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                await resp.write(chunk)
        await resp.write_eof()
    finally:
        downloader.cleanup_file(filepath)
    return resp


# ─── Download dispatcher ──────────────────────────────────────────────────────

async def _dispatch(uid: int, url: str, platform: str, height: int, dl_type: str):
    assert _sem is not None
    async with _sem:
        try:
            if platform == "tiktok":
                if dl_type == "audio":
                    await _dl_tiktok_audio(uid, url)
                else:
                    await _dl_tiktok(uid, url)
            elif platform == "facebook":
                await _dl_facebook(uid, url)
            elif platform == "youtube":
                if dl_type == "audio":
                    await _dl_youtube_audio(uid, url)
                elif dl_type == "thumb":
                    await _dl_youtube_thumb(uid, url)
                else:
                    await _dl_youtube(uid, url, height)
        except Exception as exc:
            log.error(f"[MiniApp] dispatch error uid={uid}: {exc}")
            try:
                await _bot.send_message(uid, f"❌ ဒေါင်းမရပါ — {exc}")
            except Exception:
                pass


# ─── TikTok video ─────────────────────────────────────────────────────────────

async def _dl_tiktok(uid: int, url: str):
    notice = await _bot.send_message(uid, "⏳ TikTok video ဒေါင်းနေသည်…")
    try:
        data = await downloader.fetch_tiktok_data(url)
        vurl = data.get("play", "")
        size_mb = (data.get("size") or 0) / (1024 * 1024)
        title   = data.get("title", "TikTok Video")

        if not vurl:
            return await notice.edit_text("❌ TikTok video URL ရှာမတွေ့ပါ")

        if size_mb > fb.MAX_TG_SIZE_MB:
            return await notice.edit_text(
                f"⚠️ <b>ဖိုင် {size_mb:.1f} MB ကြီး — Direct Link</b>\n\n"
                f"🔗 <a href=\"{vurl}\">ဒေါင်းရန် နှိပ်ပါ</a>\n"
                "<i>Browser ဖြင့် Save လုပ်ပါ</i>",
                parse_mode="HTML",
            )

        tmp = os.path.join(downloader.TEMP_DIR, f"ma_tt_{uid}_{int(time.time())}.mp4")
        try:
            await downloader.download_to_file(vurl, tmp)
            await _bot.send_video(uid, video=FSInputFile(tmp, filename="tiktok.mp4"),
                                  caption=f"🔥 {title[:200]}")
            await notice.delete()
        finally:
            downloader.cleanup_file(tmp)

    except Exception as exc:
        log.error(f"[MiniApp] tiktok video uid={uid}: {exc}")
        await _safe_edit(notice, f"❌ TikTok ဒေါင်းမရပါ — {exc}")


# ─── TikTok audio ─────────────────────────────────────────────────────────────

async def _dl_tiktok_audio(uid: int, url: str):
    notice = await _bot.send_message(uid, "⏳ TikTok audio ဒေါင်းနေသည်…")
    try:
        data  = await downloader.fetch_tiktok_data(url)
        music = data.get("music", "")
        title = data.get("title", "TikTok Audio")

        if not music:
            return await notice.edit_text("❌ Audio URL ရှာမတွေ့ပါ")

        await _bot.send_audio(uid, audio=music,
                              caption=f"🎵 {title[:200]}")
        await notice.delete()

    except Exception as exc:
        log.error(f"[MiniApp] tiktok audio uid={uid}: {exc}")
        await _safe_edit(notice, f"❌ TikTok Audio ဒေါင်းမရပါ — {exc}")


# ─── Facebook video ───────────────────────────────────────────────────────────

async def _dl_facebook(uid: int, url: str):
    notice = await _bot.send_message(uid, "⏳ Facebook video ဒေါင်းနေသည်…")
    try:
        result = await fb.download_facebook_video(url, uid, int(time.time()), vip_mode=False)

        if not result.ok:
            return await _safe_edit(notice, f"❌ {result.user_msg}")

        if result.size_mb and result.size_mb > fb.MAX_TG_SIZE_MB:
            direct = await fb.get_direct_url(url)
            if result.filepath:
                downloader.cleanup_file(result.filepath)
            if direct:
                return await notice.edit_text(
                    f"⚠️ <b>ဖိုင် {result.size_mb:.1f} MB ကြီး — Direct Link</b>\n\n"
                    f"🔗 <a href=\"{direct}\">ဒေါင်းရန် နှိပ်ပါ</a>\n"
                    "<i>Browser ဖြင့် Save လုပ်ပါ</i>",
                    parse_mode="HTML",
                )
            return await _safe_edit(notice, f"⚠️ ဖိုင် {result.size_mb:.1f} MB ကြီး — Telegram သို့ ပို့မရပါ")

        try:
            ext   = os.path.splitext(result.filepath)[1] or ".mp4"
            title = result.title or "Facebook Video"
            sz    = f"{result.size_mb:.1f} MB" if result.size_mb else ""
            await _bot.send_video(uid, video=FSInputFile(result.filepath, filename=f"fb{ext}"),
                                  caption=f"📘 {title[:200]}\n📦 {sz}")
            await notice.delete()
        finally:
            if result.filepath:
                downloader.cleanup_file(result.filepath)

    except Exception as exc:
        log.error(f"[MiniApp] facebook uid={uid}: {exc}")
        await _safe_edit(notice, f"❌ Facebook ဒေါင်းမရပါ — {exc}")


# ─── YouTube video ────────────────────────────────────────────────────────────

async def _dl_youtube(uid: int, url: str, height: int):
    note_txt = f"⏳ YouTube {'%dp' % height if height else 'best'} ဒေါင်းနေသည်…"
    notice   = await _bot.send_message(uid, note_txt)
    try:
        info   = await yt.fetch_youtube_info(url)
        chosen = next((f for f in (info.formats or []) if f.height == height), None) if height else None
        est_mb = chosen.size_mb if chosen else 0.0

        if est_mb > yt.MAX_TG_SIZE_MB:
            await _safe_edit(notice, f"⏳ {height}p CDN link ရယူနေသည်…")
            stream_url, note = await yt.get_direct_url(url, height)
            if stream_url:
                return await notice.edit_text(
                    f"⚠️ <b>ဖိုင် ~{est_mb:.0f} MB ကြီး — CDN Link</b>\n\n"
                    f"🔗 <a href=\"{stream_url}\">ဒေါင်းရန် နှိပ်ပါ ({note})</a>\n"
                    "<i>Link ယာယီဖြစ်သဖြင့် Browser ဖြင့် Save လုပ်ပါ</i>",
                    parse_mode="HTML",
                )
            return await _safe_edit(notice, f"⚠️ ဖိုင် ~{est_mb:.0f} MB ကြီး — Resolution နိမ့်ချပြီး ထပ်ကြိုးစားပါ")

        result = await yt.download_youtube_video(url, uid, int(time.time()), height)
        if not result.ok:
            return await _safe_edit(notice, f"❌ {result.user_msg}")

        if result.size_mb > yt.MAX_TG_SIZE_MB:
            if result.filepath:
                downloader.cleanup_file(result.filepath)
            stream_url, note = await yt.get_direct_url(url, height)
            if stream_url:
                return await notice.edit_text(
                    f"⚠️ <b>ဖိုင် {result.size_mb:.1f} MB ကြီး — CDN Link</b>\n\n"
                    f"🔗 <a href=\"{stream_url}\">ဒေါင်းရန် နှိပ်ပါ ({note})</a>",
                    parse_mode="HTML",
                )
            return await _safe_edit(notice, "⚠️ ဖိုင်ကြီးသဖြင့် ပို့မရပါ")

        try:
            title = info.title if info.ok else "YouTube Video"
            await _bot.send_video(
                uid,
                video=FSInputFile(result.filepath, filename="youtube.mp4"),
                caption=f"🎬 {title[:200]}\n🎞 {result.format_note}  📦 {result.size_mb:.1f} MB",
            )
            await notice.delete()
        finally:
            if result.filepath:
                downloader.cleanup_file(result.filepath)

    except Exception as exc:
        log.error(f"[MiniApp] youtube uid={uid}: {exc}")
        await _safe_edit(notice, f"❌ YouTube ဒေါင်းမရပါ — {exc}")


# ─── YouTube audio ────────────────────────────────────────────────────────────

def _yt_audio_sync(url: str, tpl: str) -> str | None:
    """Download YouTube audio in a thread. Returns file path or None."""
    opts: dict = {
        "format":      "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl":     tpl,
        "quiet":       True,
        "no_warnings": True,
        "noplaylist":  True,
    }
    if _ffmpeg:
        opts["ffmpeg_location"] = _ffmpeg
        opts["postprocessors"]  = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base = os.path.splitext(ydl.prepare_filename(info))[0]
    except Exception as e:
        log.error(f"[MiniApp] _yt_audio_sync error: {e}")
        return None

    for ext in (".mp3", ".m4a", ".opus", ".webm", ".ogg"):
        p = base + ext
        if os.path.exists(p):
            return p
    matches = _glob.glob(base + ".*")
    return matches[0] if matches else None


async def _dl_youtube_audio(uid: int, url: str):
    notice = await _bot.send_message(uid, "⏳ YouTube audio ဒေါင်းနေသည်…")
    tpl    = os.path.join(downloader.TEMP_DIR, f"ma_yta_{uid}_{int(time.time())}.%(ext)s")
    try:
        info = await yt.fetch_youtube_info(url)
        fp   = await asyncio.get_event_loop().run_in_executor(None, _yt_audio_sync, url, tpl)
        if not fp:
            return await _safe_edit(notice, "❌ YouTube Audio ဒေါင်းမရပါ")
        try:
            title = info.title if info.ok else "YouTube Audio"
            ext   = os.path.splitext(fp)[1].lstrip(".") or "mp3"
            await _bot.send_audio(
                uid,
                audio=FSInputFile(fp, filename=f"audio.{ext}"),
                caption=f"🎵 {title[:200]}",
            )
            await notice.delete()
        finally:
            downloader.cleanup_file(fp)
    except Exception as exc:
        log.error(f"[MiniApp] yt audio uid={uid}: {exc}")
        await _safe_edit(notice, f"❌ YouTube Audio ဒေါင်းမရပါ — {exc}")


# ─── YouTube thumbnail ────────────────────────────────────────────────────────

async def _dl_youtube_thumb(uid: int, url: str):
    notice = await _bot.send_message(uid, "⏳ YouTube thumbnail ဒေါင်းနေသည်…")
    try:
        info = await yt.fetch_youtube_info(url)
        if not info.ok or not info.thumbnail:
            return await _safe_edit(notice, "❌ Thumbnail ရှာမတွေ့ပါ")

        fp = await yt.download_thumbnail_from_url(info.thumbnail, uid, int(time.time()))
        if not fp:
            return await _safe_edit(notice, "❌ Thumbnail ဒေါင်းမရပါ")
        try:
            await _bot.send_photo(
                uid,
                photo=FSInputFile(fp, filename="thumbnail.jpg"),
                caption=f"🖼 {info.title[:200]}",
            )
            await notice.delete()
        finally:
            downloader.cleanup_file(fp)
    except Exception as exc:
        log.error(f"[MiniApp] yt thumb uid={uid}: {exc}")
        await _safe_edit(notice, f"❌ Thumbnail ဒေါင်းမရပါ — {exc}")


# ─── Utility ──────────────────────────────────────────────────────────────────

async def _safe_edit(msg, text: str, **kw):
    try:
        await msg.edit_text(text, **kw)
    except Exception:
        pass
