"""miniapp.py — Telegram Mini App backend.

Routes
──────
GET  /app          → serve the Mini App HTML UI
POST /api/info     → fetch video info (title, formats)
POST /api/dl       → start background download, send result to user's chat
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.parse

from aiohttp import web
from aiogram.types import FSInputFile

import database as db
import downloader
import facebook_downloader as fb
import youtube_downloader as yt
from logger import log

# ─── Module state (populated by init() before routes are registered) ──────────

_bot        = None
_bot_token  = ""
_domain     = ""

MINIAPP_MAX_CONCURRENT = 3          # cap simultaneous mini-app downloads
_semaphore: asyncio.Semaphore | None = None


def init(bot, bot_token: str, domain: str) -> None:
    global _bot, _bot_token, _domain, _semaphore
    _bot       = bot
    _bot_token = bot_token
    _domain    = domain
    _semaphore = asyncio.Semaphore(MINIAPP_MAX_CONCURRENT)
    log.info(f"[MiniApp] initialised — app URL: https://{domain}/app")


def register_routes(app: web.Application) -> None:
    app.router.add_get ("/app",       handle_app)
    app.router.add_post("/api/info",  handle_api_info)
    app.router.add_post("/api/dl",    handle_api_dl)
    log.info("[MiniApp] routes registered: /app  /api/info  /api/dl")


# ─── Telegram initData validation ─────────────────────────────────────────────

def _parse_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData via HMAC-SHA256.

    Returns the parsed user dict on success, None on failure.
    Empty initData is rejected so the API cannot be called from a browser.
    """
    if not init_data or not _bot_token:
        return None
    try:
        params: dict[str, str] = {}
        for item in init_data.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                params[k] = urllib.parse.unquote_plus(v)

        received_hash = params.pop("hash", None)
        if not received_hash:
            return None

        check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key   = hmac.new(b"WebAppData", _bot_token.encode(), hashlib.sha256).digest()
        computed     = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed, received_hash):
            log.warning("[MiniApp] initData HMAC mismatch")
            return None

        user = json.loads(params.get("user", "{}"))
        return user

    except Exception as exc:
        log.warning(f"[MiniApp] initData parse error: {exc}")
        return None


# ─── JSON helpers ──────────────────────────────────────────────────────────────

def _ok(**kw):
    return web.json_response({"ok": True, **kw})

def _err(msg: str, status: int = 400):
    return web.json_response({"ok": False, "error": msg}, status=status)

def _cors(response: web.Response) -> web.Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ─── /app — serve Mini App HTML ───────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Video Downloader</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--card:#161b22;--border:#30363d;
  --text:#e6edf3;--sub:#8b949e;
  --blue:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#f0883e;
  --tiktok:#fe2c55;--fb:#1877f2;--yt:#ff0000;
}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;padding:16px 16px 32px;max-width:520px;margin:0 auto}
h1{font-size:1.15rem;font-weight:700;text-align:center;margin-bottom:3px}
.sub{color:var(--sub);font-size:.78rem;text-align:center;margin-bottom:20px}

/* URL input */
.input-row{display:flex;gap:8px;margin-bottom:10px}
#url-input{flex:1;background:var(--card);border:1.5px solid var(--border);border-radius:10px;
  padding:12px 14px;color:var(--text);font-size:.95rem;outline:none;transition:border-color .2s;
  min-width:0}
#url-input:focus{border-color:var(--blue)}
#url-input::placeholder{color:var(--sub)}
#clear-btn{background:var(--card);border:1.5px solid var(--border);border-radius:10px;
  padding:0 14px;color:var(--sub);font-size:1rem;cursor:pointer;display:none;white-space:nowrap}
#paste-btn{width:100%;background:var(--card);border:1.5px solid var(--border);border-radius:10px;
  padding:11px;color:var(--blue);font-size:.88rem;cursor:pointer;margin-bottom:16px;
  transition:background .15s}
#paste-btn:active{background:#1c2a3d}

/* Info card */
#info-card{background:var(--card);border:1.5px solid var(--border);border-radius:12px;
  padding:14px;margin-bottom:14px;display:none}
.plat-badge{display:inline-flex;align-items:center;gap:5px;border-radius:20px;
  padding:3px 12px;font-size:.76rem;font-weight:600;margin-bottom:10px}
.b-tiktok{background:#2a0a10;color:var(--tiktok);border:1px solid var(--tiktok)}
.b-fb{background:#0a1529;color:var(--fb);border:1px solid var(--fb)}
.b-yt{background:#2a0a0a;color:var(--yt);border:1px solid var(--yt)}
#vtitle{font-size:.88rem;font-weight:500;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
#fmt-section{display:none;margin-top:12px}
.fmt-lbl{font-size:.73rem;color:var(--sub);margin-bottom:7px}
.fmt-grid{display:flex;flex-wrap:wrap;gap:8px}
.fmt-btn{background:#21262d;border:1.5px solid var(--border);border-radius:8px;
  padding:7px 14px;color:var(--text);font-size:.82rem;cursor:pointer;transition:border-color .15s}
.fmt-btn.sel,.fmt-btn:active{border-color:var(--blue);background:#1c2a3d;color:var(--blue)}
.warn{color:var(--yellow)!important;font-size:.7rem}

/* Buttons */
#info-btn,#dl-btn{width:100%;border-radius:12px;padding:14px;font-size:.95rem;
  font-weight:600;cursor:pointer;margin-bottom:12px;transition:opacity .15s;display:none}
#info-btn{background:var(--card);border:1.5px solid var(--blue);color:var(--blue)}
#info-btn:active{background:#1c2a3d}
#info-btn:disabled{opacity:.5;cursor:not-allowed}
#dl-btn{background:linear-gradient(135deg,#238636,#2ea043);border:none;color:#fff}
#dl-btn:not(:disabled):active{opacity:.85}
#dl-btn:disabled{opacity:.5;cursor:not-allowed}

/* Status */
#status{text-align:center;font-size:.86rem;padding:12px;border-radius:10px;
  display:none;line-height:1.5}
.s-proc{background:#111d2d;color:var(--blue)}
.s-ok  {background:#0d2318;color:var(--green)}
.s-err {background:#2a0d0d;color:var(--red)}
.s-link{background:#1a1a2e;color:var(--blue)}
.sp{display:inline-block;width:13px;height:13px;border:2px solid rgba(88,166,255,.3);
  border-top-color:var(--blue);border-radius:50%;animation:spin .7s linear infinite;
  vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}

/* direct-link card */
#link-card{display:none;background:var(--card);border:1.5px solid var(--border);
  border-radius:12px;padding:14px;margin-bottom:12px;text-align:center}
#link-card a{color:var(--blue);font-size:.9rem;font-weight:600;word-break:break-all}
#link-card .link-note{color:var(--sub);font-size:.74rem;margin-top:6px}
</style>
</head>
<body>
<h1>🎬 Video Downloader</h1>
<p class="sub">TikTok &nbsp;·&nbsp; Facebook &nbsp;·&nbsp; YouTube</p>

<div class="input-row">
  <input id="url-input" type="url" placeholder="URL ကူးထည့်ပါ..." autocomplete="off"/>
  <button id="clear-btn" onclick="clearUrl()">✕</button>
</div>
<button id="paste-btn" onclick="pasteUrl()">📋 Clipboard မှ URL ကူးထည့်ရန်</button>

<div id="info-card">
  <div id="plat-badge" class="plat-badge"></div>
  <div id="vtitle"></div>
  <div id="fmt-section">
    <div class="fmt-lbl">📹 Resolution ရွေးပါ:</div>
    <div class="fmt-grid" id="fmt-grid"></div>
  </div>
</div>

<button id="info-btn" onclick="fetchInfo()">🔍 ဗီဒီယို အချက်အလက် ရယူမည်</button>
<button id="dl-btn"   onclick="startDl()">⬇️ Download &amp; Chat ထဲပို့မည်</button>

<div id="link-card">
  <div id="link-anchor"></div>
  <div class="link-note">⚠️ Link သည် ယာယီဖြစ်သဖြင့် မကြာမီ expire ဖြစ်မည်<br>Browser / Download Manager ဖြင့် Save လုပ်ပါ</div>
</div>

<div id="status"></div>

<script>
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand();

const inp    = document.getElementById("url-input");
const clrBtn = document.getElementById("clear-btn");
let platform = null, selectedH = 0, videoInfo = null;
const initData = tg.initData || "";

inp.addEventListener("input", onUrlChange);

function onUrlChange(){
  const v = inp.value.trim();
  clrBtn.style.display = v ? "block" : "none";
  hide("info-card"); hide("link-card");
  setStatus(""); hide("dl-btn"); hide("info-btn");
  videoInfo = null; selectedH = 0;
  if(!v){ return; }
  platform = detect(v);
  if(platform==="tiktok"||platform==="facebook"){ show("dl-btn"); }
  else if(platform==="youtube"){ show("info-btn"); }
}

function detect(u){
  if(/tiktok\\.com|vm\\.tiktok/i.test(u)) return "tiktok";
  if(/facebook\\.com|fb\\.watch/i.test(u)) return "facebook";
  if(/youtube\\.com|youtu\\.be/i.test(u)) return "youtube";
  return null;
}

function clearUrl(){ inp.value=""; onUrlChange(); inp.focus(); }

async function pasteUrl(){
  try{
    const t = await navigator.clipboard.readText();
    if(t){ inp.value=t; onUrlChange(); }
  } catch{ inp.focus(); }
}

async function fetchInfo(){
  const url = inp.value.trim();
  if(!url) return;
  document.getElementById("info-btn").disabled = true;
  document.getElementById("info-btn").textContent = "⏳ ရယူနေသည်...";
  setStatus("proc","ဗီဒီယို အချက်အလက် ရယူနေသည်...");
  hide("link-card");
  try{
    const r = await post("/api/info",{url,init_data:initData});
    if(!r.ok) throw new Error(r.error||"ရယူမရပါ");
    videoInfo = r;
    showInfoCard(r);
    setStatus("");
  } catch(e){
    setStatus("err","❌ "+e.message);
  } finally{
    document.getElementById("info-btn").disabled=false;
    document.getElementById("info-btn").textContent="🔍 ဗီဒီယို အချက်အလက် ရယူမည်";
  }
}

function showInfoCard(d){
  const labels={tiktok:["b-tiktok","🎵 TikTok"],facebook:["b-fb","📘 Facebook"],youtube:["b-yt","▶️ YouTube"]};
  const [cls,txt]=labels[d.platform]||["",""];
  const badge=document.getElementById("plat-badge");
  badge.className="plat-badge "+(cls||"");
  badge.textContent=txt;
  document.getElementById("vtitle").textContent=d.title||"";
  const fs=document.getElementById("fmt-section");
  const fg=document.getElementById("fmt-grid");
  if(d.formats&&d.formats.length){
    fg.innerHTML="";
    d.formats.forEach(f=>{
      const btn=document.createElement("button");
      btn.className="fmt-btn";
      const big=f.size_mb>50;
      const sz=f.size_mb>0?" (~"+Math.round(f.size_mb)+" MB"+(big?" ⚠️":"")+")" : "";
      btn.innerHTML=f.label+sz+(big?"<br><span class='warn'>CDN link ပေးမည်</span>":"");
      btn.dataset.h=f.height;
      btn.onclick=()=>{
        document.querySelectorAll(".fmt-btn").forEach(b=>b.classList.remove("sel"));
        btn.classList.add("sel"); selectedH=f.height; show("dl-btn");
      };
      fg.appendChild(btn);
    });
    fs.style.display="block";
    hide("dl-btn");
  } else {
    fs.style.display="none";
    show("dl-btn");
  }
  show("info-card");
  hide("info-btn");
}

async function startDl(){
  const url=inp.value.trim();
  if(!url) return;
  const dlBtn=document.getElementById("dl-btn");
  dlBtn.disabled=true;
  hide("link-card");
  setStatus("proc",'<span class="sp"></span>ဒေါင်းနေသည်… Telegram chat ထဲ ပေးပို့မည်');
  try{
    const r=await post("/api/dl",{url,platform,height:selectedH,init_data:initData});
    if(!r.ok) throw new Error(r.error||"Download မအောင်မြင်ပါ");
    if(r.link){
      showLinkCard(r.link);
      setStatus("ok","✅ CDN link ရရှိပြီ — ဒေါင်းရန် အောက်ပါ link နှိပ်ပါ");
    } else {
      setStatus("ok","✅ "+(r.message||"Chat ထဲ ပေးပို့ပြီးပါပြီ！"));
    }
  } catch(e){
    setStatus("err","❌ "+e.message);
  } finally{
    dlBtn.disabled=false;
  }
}

function showLinkCard(link){
  const c=document.getElementById("link-card");
  document.getElementById("link-anchor").innerHTML='<a href="'+link+'" target="_blank">⬇️ ဒေါင်းရန် နှိပ်ပါ</a>';
  c.style.display="block";
}

async function post(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return r.json();
}

function setStatus(type,msg){
  const el=document.getElementById("status");
  if(!type||!msg){el.style.display="none";el.className="";return;}
  el.className=type==="proc"?"s-proc":type==="ok"?"s-ok":type==="link"?"s-link":"s-err";
  el.innerHTML=msg; el.style.display="block";
}
function show(id){document.getElementById(id).style.display="";}
function hide(id){document.getElementById(id).style.display="none";}
</script>
</body>
</html>
"""


async def handle_app(request: web.Request) -> web.Response:
    return _cors(web.Response(text=_HTML, content_type="text/html"))


# ─── /api/info ────────────────────────────────────────────────────────────────

async def handle_api_info(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    url       = (body.get("url") or "").strip()
    init_data = body.get("init_data", "")

    user = _parse_init_data(init_data)
    if user is None:
        return _err("Telegram auth failed — please open from the bot", 401)

    if not url:
        return _err("URL required")

    if yt.is_youtube_url(url):
        info = await yt.fetch_youtube_info(url)
        if not info.ok:
            return _err("YouTube info ရယူမရပါ — " + (info.error_msg or "Unknown error"))
        return _cors(_ok(
            platform="youtube",
            title=info.title,
            formats=[{"height": f.height, "label": f.label, "size_mb": f.size_mb}
                     for f in info.formats],
        ))

    if fb.is_facebook_url(url):
        return _cors(_ok(platform="facebook", title="Facebook Video", formats=[]))

    if downloader.TIKTOK_PATTERN.search(url):
        return _cors(_ok(platform="tiktok", title="TikTok Video", formats=[]))

    return _err("ပံ့ပိုးထားသော URL မဟုတ်ပါ (TikTok / Facebook / YouTube)")


# ─── /api/dl ──────────────────────────────────────────────────────────────────

async def handle_api_dl(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON")

    url       = (body.get("url") or "").strip()
    platform  = (body.get("platform") or "").strip()
    height    = int(body.get("height") or 0)
    init_data = body.get("init_data", "")

    user = _parse_init_data(init_data)
    if user is None:
        return _err("Telegram auth failed — please open from the bot", 401)

    uid = user.get("id")
    if not uid:
        return _err("User ID missing in initData", 401)

    if db.is_banned(uid):
        return _err("⛔ သင်သည် Bot ကို အသုံးပြုခွင့် ပိတ်ထားသည်")

    if not url:
        return _err("URL required")

    # Auto-detect platform if not provided
    if not platform:
        if yt.is_youtube_url(url):
            platform = "youtube"
        elif fb.is_facebook_url(url):
            platform = "facebook"
        elif downloader.TIKTOK_PATTERN.search(url):
            platform = "tiktok"
        else:
            return _err("ပံ့ပိုးထားသော URL မဟုတ်ပါ")

    # For YouTube large-file pre-check: return CDN link immediately
    if platform == "youtube" and height:
        # find the format's estimated size
        pass  # handled inside _do_download_yt — always async

    # Fire and forget
    asyncio.create_task(_do_download(uid, url, platform, height))
    return _cors(_ok(message="ဒေါင်းနေသည်… Telegram chat ထဲ မကြာမီ ပေးပို့မည်"))


# ─── Background download dispatcher ──────────────────────────────────────────

async def _do_download(uid: int, url: str, platform: str, height: int) -> None:
    assert _semaphore is not None
    async with _semaphore:
        try:
            if platform == "tiktok":
                await _dl_tiktok(uid, url)
            elif platform == "facebook":
                await _dl_facebook(uid, url)
            elif platform == "youtube":
                await _dl_youtube(uid, url, height)
            else:
                await _bot.send_message(uid, "❌ ပံ့ပိုးထားသော Platform မဟုတ်ပါ")
        except Exception as exc:
            log.error(f"[MiniApp] _do_download error uid={uid}: {exc}")
            try:
                await _bot.send_message(uid, f"❌ Download မအောင်မြင်ပါ — {exc}")
            except Exception:
                pass


# ─── TikTok ───────────────────────────────────────────────────────────────────

async def _dl_tiktok(uid: int, url: str) -> None:
    notice = await _bot.send_message(uid, "⏳ TikTok ဒေါင်းနေသည်...")
    try:
        data = await downloader.fetch_tiktok_data(url)
        video_url    = data.get("play") or data.get("video", {}).get("play_addr", {}).get("url_list", [""])[0]
        video_size_mb = (data.get("size") or 0) / (1024 * 1024)
        title        = data.get("title", "TikTok Video")

        if not video_url:
            await notice.edit_text("❌ TikTok video URL ရှာမတွေ့ပါ")
            return

        if video_size_mb > fb.MAX_TG_SIZE_MB:
            await notice.edit_text(
                f"⚠️ <b>ဖိုင် {video_size_mb:.1f} MB ကြီးသဖြင့် Direct Link ပေးပါသည်</b>\n\n"
                f"🔗 <a href=\"{video_url}\">ဒေါင်းရန် နှိပ်ပါ</a>\n\n"
                "<i>Browser / Download Manager ဖြင့် Save လုပ်ပါ</i>",
                parse_mode="HTML",
            )
            return

        temp = os.path.join(downloader.TEMP_DIR, f"ma_tt_{uid}_{int(time.time())}.mp4")
        try:
            await downloader.download_to_file(video_url, temp)
            f = FSInputFile(temp, filename="tiktok.mp4")
            await _bot.send_video(uid, video=f, caption=f"🔥 {title[:200]}")
            await notice.delete()
        finally:
            downloader.cleanup_file(temp)

    except Exception as exc:
        log.error(f"[MiniApp] TikTok error uid={uid}: {exc}")
        await notice.edit_text(f"❌ TikTok ဒေါင်းမရပါ — {exc}")


# ─── Facebook ─────────────────────────────────────────────────────────────────

async def _dl_facebook(uid: int, url: str) -> None:
    notice = await _bot.send_message(uid, "⏳ Facebook video ဒေါင်းနေသည်...")
    try:
        result = await fb.download_facebook_video(url, uid, int(time.time()), vip_mode=False)

        if not result.ok:
            await notice.edit_text(f"❌ {result.user_msg}")
            return

        if result.size_mb and result.size_mb > fb.MAX_TG_SIZE_MB:
            direct = await fb.get_direct_url(url)
            if direct:
                await notice.edit_text(
                    f"⚠️ <b>ဖိုင် {result.size_mb:.1f} MB ကြီး — Direct Link ပေးပါသည်</b>\n\n"
                    f"🔗 <a href=\"{direct}\">ဒေါင်းရန် နှိပ်ပါ</a>\n\n"
                    "<i>Browser / Download Manager ဖြင့် Save လုပ်ပါ</i>",
                    parse_mode="HTML",
                )
            else:
                await notice.edit_text(
                    f"⚠️ ဖိုင် {result.size_mb:.1f} MB ကြီးသဖြင့် Telegram သို့ ပို့မရပါ"
                )
            if result.filepath:
                downloader.cleanup_file(result.filepath)
            return

        try:
            ext   = os.path.splitext(result.filepath)[1] or ".mp4"
            title = result.title or "Facebook Video"
            size  = f"{result.size_mb:.1f} MB" if result.size_mb else ""
            f = FSInputFile(result.filepath, filename=f"facebook{ext}")
            await _bot.send_video(
                uid, video=f,
                caption=f"📘 {title[:200]}\n📦 {size}"
            )
            await notice.delete()
        finally:
            if result.filepath:
                downloader.cleanup_file(result.filepath)

    except Exception as exc:
        log.error(f"[MiniApp] Facebook error uid={uid}: {exc}")
        try:
            await notice.edit_text(f"❌ Facebook ဒေါင်းမရပါ — {exc}")
        except Exception:
            pass


# ─── YouTube ──────────────────────────────────────────────────────────────────

async def _dl_youtube(uid: int, url: str, height: int) -> None:
    notice = await _bot.send_message(
        uid,
        f"⏳ YouTube {'%dp' % height if height else 'best'} ဒေါင်းနေသည်..."
    )
    try:
        # Fetch info to check estimated size
        info = await yt.fetch_youtube_info(url)
        chosen = next((f for f in (info.formats or []) if f.height == height), None) if height else None
        est_mb = chosen.size_mb if chosen else 0.0

        if est_mb > yt.MAX_TG_SIZE_MB:
            await notice.edit_text(f"⏳ {height}p CDN link ရယူနေသည်...")
            stream_url, note = await yt.get_direct_url(url, height)
            if stream_url:
                await notice.edit_text(
                    f"⚠️ <b>ဖိုင် ~{est_mb:.0f} MB ကြီး — CDN Link ပေးပါသည်</b>\n\n"
                    f"🔗 <a href=\"{stream_url}\">ဒေါင်းရန် နှိပ်ပါ ({note})</a>\n\n"
                    "<i>Link သည် ယာယီဖြစ်သဖြင့် မကြာမီ expire ဖြစ်မည်</i>\n"
                    "📱 Browser / Download Manager ဖြင့် Save လုပ်ပါ",
                    parse_mode="HTML",
                )
            else:
                await notice.edit_text(
                    f"⚠️ ဖိုင် ~{est_mb:.0f} MB ကြီးသဖြင့် Telegram သို့ ပို့မရပါ\n"
                    "Resolution နိမ့်ချပြီး ထပ်ကြိုးစားပါ"
                )
            return

        result = await yt.download_youtube_video(url, uid, int(time.time()), height)

        if not result.ok:
            await notice.edit_text(f"❌ {result.user_msg}")
            return

        if result.size_mb > yt.MAX_TG_SIZE_MB:
            if result.filepath:
                downloader.cleanup_file(result.filepath)
            stream_url, note = await yt.get_direct_url(url, height)
            if stream_url:
                await notice.edit_text(
                    f"⚠️ <b>ဖိုင် {result.size_mb:.1f} MB ကြီး — CDN Link ပေးပါသည်</b>\n\n"
                    f"🔗 <a href=\"{stream_url}\">ဒေါင်းရန် နှိပ်ပါ ({note})</a>\n\n"
                    "<i>Link သည် ယာယီဖြစ်သဖြင့် မကြာမီ expire ဖြစ်မည်</i>",
                    parse_mode="HTML",
                )
            else:
                await notice.edit_text("⚠️ ဖိုင်ကြီးသဖြင့် ပို့မရပါ — Resolution နိမ့်ချပြီး ထပ်ကြိုးစားပါ")
            return

        try:
            title    = info.title if info.ok else "YouTube Video"
            size_tag = f"{result.size_mb:.1f} MB"
            note_tag = result.format_note or ""
            f = FSInputFile(result.filepath, filename="youtube.mp4")
            await _bot.send_video(
                uid, video=f,
                caption=f"🎬 {title[:200]}\n🎞 {note_tag}  📦 {size_tag}"
            )
            await notice.delete()
        finally:
            if result.filepath:
                downloader.cleanup_file(result.filepath)

    except Exception as exc:
        log.error(f"[MiniApp] YouTube error uid={uid}: {exc}")
        try:
            await notice.edit_text(f"❌ YouTube ဒေါင်းမရပါ — {exc}")
        except Exception:
            pass
