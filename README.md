# BayreuthUkraine Anti-Spam Bot

Telegram moderation bot with multi-group and multi-moderator isolation.

## Core model

- Group can be auto-registered when bot is added to a group (bound to the user who added it).
- Manual registration via `/register_group` is still available.
- Every registered group has its own:
  - moderators
  - whitelist
  - hard-block custom keywords
  - review-alert toggle
- Alerts are sent only to moderators of the same group, in private chat with bot.
- Moderators of group A do not receive data from group B.

## Project structure

- `main/main.py` - app entrypoint
- `main/bot/config.py` - env/configuration
- `main/bot/db.py` - sqlite storage and isolation logic
- `main/bot/rules.py` - keywords and regex patterns
- `main/bot/state.py` - in-memory spam/reputation state (per group)
- `main/bot/models.py` - moderation models/statuses
- `main/bot/classifier.py` - classification logic
- `main/bot/actions.py` - delete/ban/notify actions
- `main/bot/handlers.py` - aiogram handlers/router

## Run

1. Create `.env` from `.env.example` and set `BOT_TOKEN`.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Start bot:
   - `python main/main.py`

## Key env vars

- `BOT_TOKEN` - Telegram bot token
- `TEST_MODE` - `1/0`; when `1` bot does not delete/ban
- `SUSPECT_SCORE_THRESHOLD` - suspect threshold
- `BLOCK_SCORE_THRESHOLD` - block threshold
- `SUSPECT_ESCALATION_COUNT` - suspect repeats before auto-block
- `WINDOW_SECONDS`, `FLOOD_COUNT`, `DUPLICATE_WINDOW_SECONDS` - spam pattern tuning

## Commands

### Basic

- `/start`
- `/my_id`
- `/chat_id`
- `/mod_help`
- `/my_groups`

### Group registration

- `/register_group` (run in target group; only group admin)
- `/delete_group [group_id]` (only group creator who registered it)

### Moderators

- `/add_moderator <user_id> [group_id|all]`
- `/remove_moderator <user_id> [group_id|all]`
- `/list_moderators [group_id]`

### Lists

- `/add_whitelist <user_id> [group_id|all]`
- `/remove_whitelist <user_id> [group_id|all]`
- `/list_whitelist [group_id]`

### Hard-block custom words

- `/add_hardword <word or phrase> [group_id|all]`
- `/remove_hardword <word or phrase> [group_id|all]`
- `/list_hardwords [group_id]`

If phrase has spaces and you need explicit groups, use delimiter:
- `/add_hardword some phrase | all`

### Alerts

- `/set_review_alerts <on|off> [group_id|all]`
- `/pause_group [group_id|all]`
- `/resume_group [group_id|all]`

## Important behavior

- `whitelist` = trusted advertiser in that group:
  - no review/pending push notifications for their non-critical ads
  - hard-block (critical) ads are still deleted and moderators are notified

### Add moderator flow

1. New moderator opens bot in private and sends `/start`.
2. New moderator sends you their id from `/my_id`.
3. Current moderator adds them via `/add_moderator <user_id> [group_id|all]`.

## Security

- Never commit `.env` or real bot tokens.
- If token was posted publicly, revoke it in `@BotFather` and generate a new one.
