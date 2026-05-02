# TikTok / Facebook / YouTube Downloader Telegram Bot

A Telegram bot built with Python and `aiogram` v3 that downloads TikTok, Facebook, and YouTube videos. Uses tikwm.com API for TikTok, yt-dlp for Facebook and YouTube. Deployed via aiohttp webhook server on port 8080.

## Architecture

- **main.py** — Bot entry point; all message/callback handlers; webhook server
- **downloader.py** — TikTok URL validation, API fetch, video/image download, typed error handling
- **facebook_downloader.py** — Facebook video download via yt-dlp; FREE and VIP quality modes
- **youtube_downloader.py** — YouTube video/thumbnail download via yt-dlp; resolution picker; YTVideoInfo / YTDownloadResult containers
- **logger.py** — Logging setup (console + `logs/bot.log`) and admin alert helper
- **database.py** — SQLite-backed storage (`bot_data.db`); 16 tables; migrates legacy txt files on first run
- **admin_panel.py** — Admin keyboard, stats report, CSV export, DB backup helpers
- **keep_alive.py** — Flask keep-alive server (port 8080)
- **roles.py** — Multi-role permission system: owner / admin / support
- **cooldown.py** — Per-user 30s download cooldown; role holders bypass
- **referral.py** — Referral link system with tiered rewards (ad-skip, premium 1d, premium 30d)
- **settings.py** — Feature flags, threshold lock system, monetization quota helpers
- **tasks.py** — Task/sponsor system: task CRUD, completion logic, reward logic
- **broadcaster.py** — Scheduled/repeat broadcast system with auto-delete
- **bot_data.db** — SQLite database (16 tables)
- **temp_media/** — Temporary download files (auto-created, auto-deleted after each send)

## DB Tables (16)

`users`, `banned_users`, `downloads`, `admin_roles`, `referrals`, `premium`, `rewards`,
`settings`, `daily_usage`, `payment_accounts`, `premium_plans`, `user_usage`,
`tasks`, `user_tasks`, `broadcasts`, `broadcast_deliveries`, `yt_daily_usage`

## Feature Flags

| Flag | Default | Threshold |
|------|---------|-----------|
| cooldown_enabled | ON | none |
| force_join_enabled | OFF | 500 users |
| premium_enabled | OFF | 1000 users |
| monetization_enabled | OFF | 1000 users |
| ad_system_enabled | OFF | 1000 users |
| business_layer_enabled | OFF | 1000 users |
| pro_features_enabled | OFF | 1000 users |
| task_system_enabled | OFF | 5000 users |
| sponsor_mode_enabled | OFF | 5000 users |

## Environment Variables Required

- `BOT_TOKEN` — Telegram Bot API token
- `ADMIN_ID` — Telegram user ID of the owner

## Key Features

- Downloads TikTok videos (watermark-free, up to 100MB sent directly; larger → link)
- Photo carousel support — images downloaded locally with proper CDN headers before upload
- Audio MP3 extraction via inline button
- Facebook video download: FREE (720p mp4) and VIP (original quality, document)
- **YouTube download** (always active, separate daily quota):
  - Resolution selection keyboard (up to 6 options: 1080p down to lowest)
  - Thumbnail-only download button
  - FREE: 1 video/day per user
  - Ad-unlock: 1 extra video/day (if `ad_system_enabled` ON)
  - Premium/VIP: unlimited, sent as document (no Telegram compression)
  - Separate `yt_daily_usage` DB table tracks per-user daily YouTube usage
- Multi-role admin panel (owner/admin/support) with stats, broadcast, ban, CSV exports, DB backup
- Per-user 30s cooldown; role bypass
- Referral/reward system (3 tiers)
- Feature flag + threshold lock system
- Daily download quota with ad-unlock (TikTok/Facebook monetization)
- Payment account & premium plan management
- Task/Sponsor system (locked until 5000 users)
- Scheduled/repeat broadcast with auto-delete

## Running

The workflow runs `bash run.sh` which auto-restarts `python main.py` on crash.
Webhook mode is auto-detected when `REPLIT_DOMAINS` is set.

## Known Behaviour

- Admin/Staff cannot download videos (intentional restriction)
- All captions truncated to Telegram's 1024-char limit via `_cap()` helper
- Image CDN URLs downloaded locally first (required headers: Referer + User-Agent)
- YouTube downloads use yt-dlp thread executor (blocking I/O off event loop)
- Port 8080 used by aiohttp webhook server; Flask keep_alive only runs in polling mode
