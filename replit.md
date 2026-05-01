# TikTok Downloader Telegram Bot

A Telegram bot built with Python and `aiogram` v3 that downloads TikTok videos (without watermark), audio, and photo carousels. Uses tikwm.com API. Deployed via aiohttp webhook server on port 8080.

## Architecture

- **main.py** — Bot entry point; all message/callback handlers; webhook server
- **downloader.py** — TikTok URL validation, API fetch, video/image download, typed error handling
- **logger.py** — Logging setup (console + `logs/bot.log`) and admin alert helper
- **database.py** — SQLite-backed storage (`bot_data.db`); 15 tables; migrates legacy txt files on first run
- **admin_panel.py** — Admin keyboard, stats report, CSV export, DB backup helpers
- **keep_alive.py** — Flask keep-alive server (port 8080)
- **roles.py** — Multi-role permission system: owner / admin / support
- **cooldown.py** — Per-user 30s download cooldown; role holders bypass
- **referral.py** — Referral link system with tiered rewards (ad-skip, premium 1d, premium 30d)
- **settings.py** — Feature flags, threshold lock system, monetization quota helpers
- **tasks.py** — Task/sponsor system (Batch 7): task CRUD, completion logic, reward logic
- **bot_data.db** — SQLite database (15 tables)
- **temp_media/** — Temporary download files (auto-created, auto-deleted after each send)
- **exports/** — Temporary CSV/backup files during admin exports (auto-deleted after sending)

## DB Tables (15)

`users`, `banned_users`, `downloads`, `admin_roles`, `referrals`, `premium`, `rewards`,
`settings`, `daily_usage`, `payment_accounts`, `premium_plans`, `user_usage`,
`tasks`, `user_tasks` (added Batch 7)

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
- Multi-role admin panel (owner/admin/support) with stats, broadcast, ban, CSV exports, DB backup
- Per-user 30s cooldown; role bypass
- Referral/reward system (3 tiers)
- Feature flag + threshold lock system
- Daily download quota with ad-unlock (monetization system)
- Payment account & premium plan management (for future payment flow)
- **Task/Sponsor system** (Batch 7 — locked until 5000 users):
  - Admin creates tasks: join_channel / visit_link / custom_task
  - Users complete tasks to earn extra download quota
  - Channel membership verified via Telegram API for join_channel tasks
  - Duplicate-reward prevention via DB primary key constraint
  - Admin commands: /addtask /listtask /toggletask /deltask /taskstats

## Running

The workflow runs `python main.py`. Webhook mode is auto-detected when `REPLIT_DOMAINS` is set.

## Known Behaviour

- Admin/Staff cannot download videos (intentional restriction)
- All captions truncated to Telegram's 1024-char limit via `_cap()` helper
- Image CDN URLs downloaded locally first (required headers: Referer + User-Agent)
