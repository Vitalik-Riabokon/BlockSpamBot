# BayreuthUkraine Anti-Spam Bot

Telegram moderation bot with WebApp-first moderation flow.

## What it does

- Auto-registers a group when a moderator adds the bot.
- Isolates data per group:
  - moderators
  - legalised advertisers
  - custom triggers
  - notification settings
- Sends private alerts only to moderators of the same group.
- Uses Telegram for alerts and WebApp for moderation actions.

## Main components

- `main/main.py` - app entrypoint
- `main/bot/config.py` - environment/config
- `main/bot/db.py` - sqlite storage
- `main/bot/classifier.py` - ad/risk classification
- `main/bot/handlers.py` - Telegram bot handlers
- `main/bot/webapp_server.py` - WebApp backend API
- `main/bot/tunnel_notifier.py` - current public tunnel URL tracking
- `main/webapp/` - WebApp frontend

## Local run

1. Create `.env` from `.env.example`
2. Set at least `BOT_TOKEN`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Start:
   - `python main/main.py`

If you run the bot locally with polling, make sure there is no second instance with the same token.

## Docker / Docker Compose

Production layout:

- `bot` - Telegram bot + WebApp backend
- `cloudflared` - tunnel sidecar

### Start

1. Fill `.env`
2. Run:
   - `docker compose up -d --build`

### Stop

- `docker compose down`

### What compose does

- starts bot on `0.0.0.0:8080`
- starts `cloudflared` as a separate service
- writes tunnel log to `main/data/cloudflared.out.log`
- keeps sqlite data in a Docker volume
- bot reads the shared tunnel log and sends the current public URL to the owner in Telegram

### Check status

- `docker compose ps`
- `docker compose logs bot --tail=100`
- `docker compose logs cloudflared --tail=100`

### Deploy flow after tunnel URL changes

1. Bot sends you the new `trycloudflare` URL in private.
2. In `@BotFather` run `/setdomain`.
3. Paste only the domain part, without `https://`.
4. Reopen the WebApp.

### Token rotation

If you suspect another instance is still using the bot token:

1. Generate a new token in `@BotFather`
2. Update `BOT_TOKEN` in `.env`
3. Restart the stack:
   - `docker compose down`
   - `docker compose up -d --build`

## Important env vars

- `BOT_TOKEN` - Telegram bot token
- `TEST_MODE` - when `1`, bot analyzes but does not perform destructive moderation actions
- `WEBAPP_HOST`, `WEBAPP_PORT` - local bind for WebApp backend
- `WEBAPP_BASE_URL` - optional fallback public URL; normal tunnel flow uses runtime tunnel state
- `TUNNEL_NOTIFY_ENABLED` - send tunnel URL updates in Telegram
- `TUNNEL_NOTIFY_USER_ID` - who receives tunnel URL updates
- `CLOUDFLARED_AUTO_START` - local mode only; in Docker it must stay `0`
- `CLOUDFLARED_BIN` - local cloudflared binary path/name
- `CLOUDFLARED_TARGET_URL` - target URL for cloudflared in local mode
- `TUNNEL_LOG_PATH` - shared tunnel log path
- `BOT_LOCK_PATH` - process lock file path
- `SPLIT_WINDOW_SECONDS`, `SPLIT_MAX_MESSAGES` - split-message ad detection window
- `AD_DUPLICATE_BLOCK_WINDOW_SECONDS`, `AD_DUPLICATE_BLOCK_COUNT` - duplicate-ad hard block heuristics

## Runtime behavior

- `Проблемна` alerts are always important.
- `Не санкціонована` alerts depend on group setting.
- `Легалізовані` do not send push alerts.
- `Заблоковані` alerts use their own sound setting.
- Daily cleanup removes recent service messages and posts one fresh status message.

## Security

- Do not commit `.env`.
- If a token was exposed, revoke it in `@BotFather`.
