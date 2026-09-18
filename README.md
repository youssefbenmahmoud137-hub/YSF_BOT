# YSF Bot

YSF Bot is a simple Telegram bot written in Python. It responds to `/start`
with a friendly welcome message in Tunisian Arabic.

## Configuration

The bot reads its token from the `TELEGRAM_BOT_TOKEN` environment secret.
Create or manage the token with [@BotFather](https://t.me/BotFather) on
Telegram, then add it as a Replit Secret with that exact name.

## Run

Start the `YSF Bot` workflow. The bot uses long polling, so no public webhook
URL is required. Open the bot in Telegram and send `/start`.