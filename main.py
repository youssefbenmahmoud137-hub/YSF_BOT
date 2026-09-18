import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


WELCOME_MESSAGE = """سلام 👋 ومرحبا بيك في YSF Bot 🤖
نورت البوت! استعمل الأوامر الموجودة في القائمة باش تبدأ.
نتمنالك تجربة باهية ❤️"""

DATABASE_PATH = Path(__file__).with_name("ysf_bot.db")
DAILY_COOLDOWN_SECONDS = 24 * 60 * 60
REFERRAL_PREFIX = "ref_"
SHOP_MENU_BUTTON = "🛒 متجر YSF"
SHOP_PRODUCT_CALLBACK_PREFIX = "shop:product:"
SHOP_PURCHASE_CALLBACK_PREFIX = "shop:buy:"
SHOP_MENU_CALLBACK = "shop:menu"

SHOP_PRODUCTS = {
    "diamonds_100": ("💎 100 Diamonds", "4 DT"),
    "diamonds_200": ("💎 200 Diamonds", "8 DT"),
    "diamonds_300": ("💎 300 Diamonds", "12 DT"),
    "diamonds_500": ("💎 500 Diamonds", "19 DT"),
    "weekly_membership": ("📅 Weekly membership", "2 Orange cards"),
    "monthly_membership": ("📅 Monthly membership", "10 Orange cards"),
}

logger = logging.getLogger(__name__)


class UserStore:
    """Persistent SQLite storage for points, referrals, and daily claims."""

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    points INTEGER NOT NULL DEFAULT 0,
                    referral_count INTEGER NOT NULL DEFAULT 0,
                    last_daily_claim_at INTEGER,
                    has_been_referred INTEGER NOT NULL DEFAULT 0
                        CHECK (has_been_referred IN (0, 1))
                )
                """
            )

    def ensure_user(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO users (telegram_user_id)
                VALUES (?)
                """,
                (telegram_user_id,),
            )

    def get_stats(self, telegram_user_id: int) -> tuple[int, int]:
        self.ensure_user(telegram_user_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT points, referral_count
                FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("User could not be loaded from the database.")
        return int(row["points"]), int(row["referral_count"])

    def add_referral(self, invitee_id: int, inviter_id: int) -> bool:
        """Award one point if this is the invitee's first valid referral."""
        if invitee_id == inviter_id:
            return False

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO users (telegram_user_id)
                VALUES (?)
                """,
                (invitee_id,),
            )

            inviter_exists = connection.execute(
                """
                SELECT 1
                FROM users
                WHERE telegram_user_id = ?
                """,
                (inviter_id,),
            ).fetchone()
            if inviter_exists is None:
                return False

            invitee = connection.execute(
                """
                SELECT has_been_referred
                FROM users
                WHERE telegram_user_id = ?
                """,
                (invitee_id,),
            ).fetchone()
            if invitee is None or bool(invitee["has_been_referred"]):
                return False

            updated = connection.execute(
                """
                UPDATE users
                SET points = points + 1,
                    referral_count = referral_count + 1
                WHERE telegram_user_id = ?
                """,
                (inviter_id,),
            )
            if updated.rowcount != 1:
                return False

            connection.execute(
                """
                UPDATE users
                SET has_been_referred = 1
                WHERE telegram_user_id = ?
                """,
                (invitee_id,),
            )
            return True

    def claim_daily(
        self, telegram_user_id: int, now: Optional[datetime] = None
    ) -> tuple[bool, int]:
        """Claim the daily point and return (claimed, remaining_seconds)."""
        current_time = now or datetime.now(timezone.utc)
        current_timestamp = int(current_time.timestamp())

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO users (telegram_user_id)
                VALUES (?)
                """,
                (telegram_user_id,),
            )
            row = connection.execute(
                """
                SELECT last_daily_claim_at
                FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()

            last_claim = row["last_daily_claim_at"] if row else None
            if last_claim is not None:
                elapsed = current_timestamp - int(last_claim)
                remaining = DAILY_COOLDOWN_SECONDS - elapsed
                if remaining > 0:
                    return False, remaining

            connection.execute(
                """
                UPDATE users
                SET points = points + 1,
                    last_daily_claim_at = ?
                WHERE telegram_user_id = ?
                """,
                (current_timestamp, telegram_user_id),
            )
            return True, 0


def get_store(context: ContextTypes.DEFAULT_TYPE) -> UserStore:
    store = context.application.bot_data.get("store")
    if not isinstance(store, UserStore):
        raise RuntimeError("User store is not configured.")
    return store


def get_telegram_user_id(update: Update) -> Optional[int]:
    user = update.effective_user
    return user.id if user is not None else None


def parse_referral_parameter(parameter: str) -> Optional[int]:
    if not parameter.startswith(REFERRAL_PREFIX):
        return None

    raw_user_id = parameter[len(REFERRAL_PREFIX) :]
    try:
        user_id = int(raw_user_id)
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def format_remaining_time(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} س و{remaining_minutes} د"
    return f"{remaining_minutes} د"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[SHOP_MENU_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
    )


def shop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{product_name} — {price}",
                    callback_data=f"{SHOP_PRODUCT_CALLBACK_PREFIX}{product_id}",
                )
            ]
            for product_id, (product_name, price) in SHOP_PRODUCTS.items()
        ]
    )


def product_keyboard(product_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛒 شراء",
                    callback_data=f"{SHOP_PURCHASE_CALLBACK_PREFIX}{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ رجوع للمتجر",
                    callback_data=SHOP_MENU_CALLBACK,
                )
            ],
        ]
    )


def get_product_from_callback(
    callback_data: Optional[str], prefix: str
) -> Optional[tuple[str, str, str]]:
    if not callback_data or not callback_data.startswith(prefix):
        return None

    product_id = callback_data[len(prefix) :]
    product = SHOP_PRODUCTS.get(product_id)
    if product is None:
        return None

    product_name, price = product
    return product_id, product_name, price


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register the user, process an optional referral, then welcome them."""
    if update.message is None:
        return

    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    store = get_store(context)
    store.ensure_user(user_id)

    if context.args:
        inviter_id = parse_referral_parameter(context.args[0])
        if inviter_id is not None and store.add_referral(user_id, inviter_id):
            logger.info("Processed a referral for user %s", user_id)

    await update.message.reply_text(
        WELCOME_MESSAGE,
        reply_markup=main_menu_keyboard(),
    )


async def points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    user_points, referral_count = get_store(context).get_stats(user_id)
    await update.message.reply_text(
        f"عندك توا {user_points} نقطة.\n"
        f"دعواتك الناجحة: {referral_count}."
    )


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    store = get_store(context)
    store.ensure_user(user_id)
    bot = await context.bot.get_me()
    if not bot.username:
        raise RuntimeError("The Telegram bot username is unavailable.")

    referral_link = f"https://t.me/{bot.username}?start={REFERRAL_PREFIX}{user_id}"
    await update.message.reply_text(
        f"هذا رابط الدعوة متاعك:\n{referral_link}\n"
        "كل شخص جديد يدخل من الرابط يعطيك +1 نقطة."
    )


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    claimed, remaining_seconds = get_store(context).claim_daily(user_id)
    if claimed:
        await update.message.reply_text(
            "برافو! خذيت +1 نقطة اليوم.\n"
            "ارجع غدوة باش تاخو النقطة اليومية من جديد."
        )
        return

    await update.message.reply_text(
        "النقطة اليومية خذيتها قبل.\n"
        f"استنى {format_remaining_time(remaining_seconds)} باش تعاود تاخذها."
    )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "الأوامر المتوفرة:\n"
        "/points — شوف قداش عندك نقاط\n"
        "/invite — خرّج رابط الدعوة متاعك\n"
        "/daily — خذ نقطة كل 24 ساعة\n"
        "🛒 متجر YSF — تصفّح المنتجات\n"
        "/help — شوف المساعدة"
    )


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🛒 متجر YSF\nاختار المنتج اللي تحب عليه:",
        reply_markup=shop_keyboard(),
    )


async def shop_product_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    product = get_product_from_callback(
        query.data, SHOP_PRODUCT_CALLBACK_PREFIX
    )
    if product is None:
        await query.edit_message_text(
            "المنتج هذا ما عادش موجود في المتجر."
        )
        return

    product_id, product_name, price = product
    await query.edit_message_text(
        f"{product_name}\nالسعر: {price}\n\n"
        "اضغط على شراء باش تكمل الطلب.",
        reply_markup=product_keyboard(product_id),
    )


async def shop_purchase_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    product = get_product_from_callback(
        query.data, SHOP_PURCHASE_CALLBACK_PREFIX
    )
    if product is None:
        await query.edit_message_text(
            "المنتج هذا ما عادش موجود في المتجر."
        )
        return

    _, product_name, price = product
    await query.edit_message_text(
        f"طلبك: {product_name}\nالسعر: {price}\n\n"
        "باش تكمل الطلب، تواصل مع أدمن البوت.\n"
        "الدفع والطلب يتمّوا يدويًا في الوقت الحالي."
    )


async def shop_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    await query.edit_message_text(
        "🛒 متجر YSF\nاختار المنتج اللي تحب عليه:",
        reply_markup=shop_keyboard(),
    )


async def register_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "ابدأ مع YSF Bot"),
            BotCommand("points", "شوف نقاطك"),
            BotCommand("invite", "خرّج رابط الدعوة"),
            BotCommand("daily", "خذ النقطة اليومية"),
            BotCommand("help", "شوف المساعدة"),
        ]
    )


def build_application(
    token: str, store: Optional[UserStore] = None
) -> Application:
    """Create the bot application, storage, and command handlers."""
    application = (
        Application.builder()
        .token(token)
        .post_init(register_commands)
        .build()
    )
    application.bot_data["store"] = store or UserStore()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("points", points))
    application.add_handler(CommandHandler("invite", invite))
    application.add_handler(CommandHandler("daily", daily))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & filters.Regex(f"^{re.escape(SHOP_MENU_BUTTON)}$"),
            show_shop,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            shop_product_callback,
            pattern=f"^{re.escape(SHOP_PRODUCT_CALLBACK_PREFIX)}",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            shop_purchase_callback,
            pattern=f"^{re.escape(SHOP_PURCHASE_CALLBACK_PREFIX)}",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            shop_menu_callback,
            pattern=f"^{re.escape(SHOP_MENU_CALLBACK)}$",
        )
    )
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