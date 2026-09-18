import logging
import os

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes


WELCOME_MESSAGE = """سلام 👋 ومرحبا بيك في YSF Bot 🤖
نورت البوت! استعمل الأوامر الموجودة في القائمة باش تبدأ.
نتمنالك تجربة باهية ❤️"""


logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome users when they send /start."""
    if update.message is None:
        return

    await update.message.reply_text(WELCOME_MESSAGE)


async def register_commands(application: Application) -> None:
    """Make /start visible in Telegram's bot command menu."""
    await application.bot.set_my_commands(
        [BotCommand("start", "ابدأ مع YSF Bot")]
    )


def build_application(token: str) -> Application:
    """Create the bot application and register its handlers."""
    application = (
        Application.builder()
        .token(token)
        .post_init(register_commands)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    return application


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured. "
            "Add the bot token as a Replit Secret."
        )

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logger.info("Starting YSF Bot")

    application = build_application(token)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
