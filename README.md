# YSF Bot

YSF Bot is a simple Telegram bot written in Python. It welcomes users in
Tunisian Arabic and lets them earn points through referrals and a daily reward.

## Configuration

The bot reads its token from the `TELEGRAM_BOT_TOKEN` environment secret.
Create or manage the token with [@BotFather](https://t.me/BotFather) on
Telegram, then add it as a Replit Secret with that exact name.

## Run

Start the `YSF Bot` workflow. The bot uses long polling, so no public webhook
URL is required. User points and referral data are stored in the local
`ysf_bot.db` SQLite database.

Open the bot in Telegram and use:

- `/start` — welcome message and referral processing
- `/points` — current points and successful referral count
- `/invite` — personal referral link
- `/daily` — claim one point every 24 hours
- `/help` — command help