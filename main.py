import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Optional

from flask import Flask
from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
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
SHOP_CANCEL_CALLBACK = "shop:cancel"
ORDER_COMPLETE_CALLBACK_PREFIX = "order:complete:"
ORDER_REJECT_CALLBACK_PREFIX = "order:reject:"
MAX_PAYMENT_INFO_LENGTH = 1000
MAX_REJECTION_REASON_LENGTH = 1000

SHOP_PRODUCTS = {
    "diamonds_100": ("💎 100 جوهرة", "4 د.ت"),
    "diamonds_200": ("💎 200 جوهرة", "8 د.ت"),
    "diamonds_300": ("💎 300 جوهرة", "12 د.ت"),
    "diamonds_500": ("💎 500 جوهرة", "19 د.ت"),
    "weekly_membership": ("📅 أسبوعي", "بطاقتين أورونج"),
    "monthly_membership": ("📅 شهري", "10 بطاقات أورونج"),
}

logger = logging.getLogger(__name__)

app = Flask(__name__)
HEALTH_SERVER_PORT = int(os.getenv("YSF_HEALTH_PORT", "8082"))


@app.route("/")
def home() -> str:
    return "البوت خدام!"


def run_health_server() -> None:
    app.run(host="0.0.0.0", port=HEALTH_SERVER_PORT)


class UserStore:
    """Persistent SQLite storage for users, checkouts, and orders."""

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_checkouts (
                    telegram_user_id INTEGER PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    price TEXT NOT NULL,
                    game_id TEXT,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_number INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_username TEXT,
                    product TEXT NOT NULL,
                    price TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_admin_rejections (
                    pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    order_number INTEGER NOT NULL UNIQUE,
                    admin_chat_id INTEGER NOT NULL,
                    admin_message_id INTEGER NOT NULL,
                    prompt_message_id INTEGER,
                    created_at INTEGER NOT NULL
                )
                """
            )
            rejection_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(pending_admin_rejections)"
                )
            }
            if "pending_id" not in rejection_columns:
                connection.execute(
                    """
                    CREATE TABLE pending_admin_rejections_new (
                        pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        admin_id INTEGER NOT NULL,
                        order_number INTEGER NOT NULL UNIQUE,
                        admin_chat_id INTEGER NOT NULL,
                        admin_message_id INTEGER NOT NULL,
                        prompt_message_id INTEGER,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO pending_admin_rejections_new (
                        admin_id,
                        order_number,
                        admin_chat_id,
                        admin_message_id,
                        prompt_message_id,
                        created_at
                    )
                    SELECT admin_id,
                           order_number,
                           admin_chat_id,
                           admin_message_id,
                           NULL,
                           created_at
                    FROM pending_admin_rejections
                    """
                )
                connection.execute(
                    "DROP TABLE pending_admin_rejections"
                )
                connection.execute(
                    """
                    ALTER TABLE pending_admin_rejections_new
                    RENAME TO pending_admin_rejections
                    """
                )
            order_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(orders)")
            }
            if "rejection_reason" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN rejection_reason TEXT"
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

    def start_checkout(
        self,
        telegram_user_id: int,
        product_id: str,
        product_name: str,
        price: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO pending_checkouts (
                    telegram_user_id,
                    product_id,
                    product_name,
                    price,
                    game_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    product_id = excluded.product_id,
                    product_name = excluded.product_name,
                    price = excluded.price,
                    game_id = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    telegram_user_id,
                    product_id,
                    product_name,
                    price,
                    int(datetime.now(timezone.utc).timestamp()),
                ),
            )

    def get_pending_checkout(
        self, telegram_user_id: int
    ) -> Optional[tuple[str, str, str, Optional[str]]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT product_id, product_name, price, game_id
                FROM pending_checkouts
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()

        if row is None:
            return None
        return (
            str(row["product_id"]),
            str(row["product_name"]),
            str(row["price"]),
            str(row["game_id"]) if row["game_id"] is not None else None,
        )

    def set_checkout_game_id(
        self, telegram_user_id: int, game_id: str
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE pending_checkouts
                SET game_id = ?,
                    updated_at = ?
                WHERE telegram_user_id = ?
                  AND game_id IS NULL
                """,
                (
                    game_id,
                    int(datetime.now(timezone.utc).timestamp()),
                    telegram_user_id,
                ),
            )
            return updated.rowcount == 1

    def create_order(
        self,
        telegram_user_id: int,
        telegram_username: Optional[str],
    ) -> Optional[tuple[int, str, str, str]]:
        """Create one pending order and remove its completed checkout session."""
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkout = connection.execute(
                """
                SELECT product_name AS product, price, game_id
                FROM pending_checkouts
                WHERE telegram_user_id = ?
                  AND game_id IS NOT NULL
                """,
                (telegram_user_id,),
            ).fetchone()
            if checkout is None:
                return None

            created = connection.execute(
                """
                INSERT INTO orders (
                    telegram_user_id,
                    telegram_username,
                    product,
                    price,
                    game_id,
                    created_at,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    telegram_user_id,
                    telegram_username,
                    checkout["product"],
                    checkout["price"],
                    checkout["game_id"],
                    created_at,
                ),
            )
            if created.lastrowid is None:
                raise RuntimeError("Order number was not generated.")

            connection.execute(
                """
                DELETE FROM pending_checkouts
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            )
            return (
                int(created.lastrowid),
                str(checkout["product"]),
                str(checkout["price"]),
                str(checkout["game_id"]),
            )

    def get_order(self, order_number: int) -> Optional[dict[str, object]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT order_number, telegram_user_id, telegram_username,
                       product, price, game_id, created_at, status,
                       rejection_reason
                FROM orders
                WHERE order_number = ?
                """,
                (order_number,),
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_order(self, order_number: int) -> Optional[dict[str, object]]:
        """Mark a pending order as completed exactly once."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT order_number, telegram_user_id, telegram_username,
                       product, price, game_id, created_at, status,
                       rejection_reason
                FROM orders
                WHERE order_number = ?
                """,
                (order_number,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None

            updated = connection.execute(
                """
                UPDATE orders
                SET status = 'completed'
                WHERE order_number = ?
                  AND status = 'pending'
                """,
                (order_number,),
            )
            if updated.rowcount != 1:
                return None

            connection.execute(
                """
                DELETE FROM pending_admin_rejections
                WHERE order_number = ?
                """,
                (order_number,),
            )
            completed = dict(row)
            completed["status"] = "completed"
            return completed

    def begin_rejection(
        self,
        admin_id: int,
        order_number: int,
        admin_chat_id: int,
        admin_message_id: int,
    ) -> Optional[dict[str, object]]:
        """Put one pending order into the admin's reason-collection state."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT order_number, telegram_user_id, telegram_username,
                       product, price, game_id, created_at, status,
                       rejection_reason
                FROM orders
                WHERE order_number = ?
                """,
                (order_number,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None

            connection.execute(
                """
                INSERT INTO pending_admin_rejections (
                    admin_id,
                    order_number,
                    admin_chat_id,
                    admin_message_id,
                    prompt_message_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(order_number) DO UPDATE SET
                    admin_id = excluded.admin_id,
                    admin_chat_id = excluded.admin_chat_id,
                    admin_message_id = excluded.admin_message_id,
                    created_at = excluded.created_at
                """,
                (
                    admin_id,
                    order_number,
                    admin_chat_id,
                    admin_message_id,
                    int(datetime.now(timezone.utc).timestamp()),
                ),
            )
            return dict(row)

    def set_rejection_prompt_message(
        self, admin_id: int, order_number: int, prompt_message_id: int
    ) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE pending_admin_rejections
                SET prompt_message_id = ?
                WHERE admin_id = ?
                  AND order_number = ?
                """,
                (prompt_message_id, admin_id, order_number),
            )
            return updated.rowcount == 1

    def get_pending_admin_rejections(
        self, admin_id: int
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.admin_id, p.order_number, p.admin_chat_id,
                       p.admin_message_id, p.prompt_message_id,
                       o.telegram_user_id,
                       o.telegram_username, o.product, o.price, o.game_id,
                       o.created_at, o.status, o.rejection_reason
                FROM pending_admin_rejections AS p
                JOIN orders AS o ON o.order_number = p.order_number
                WHERE p.admin_id = ?
                ORDER BY p.created_at ASC, p.pending_id ASC
                """,
                (admin_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_admin_rejection(
        self,
        admin_id: int,
        order_number: Optional[int] = None,
        reply_message_id: Optional[int] = None,
    ) -> Optional[dict[str, object]]:
        clauses = ["p.admin_id = ?"]
        parameters: list[object] = [admin_id]
        if order_number is not None:
            clauses.append("p.order_number = ?")
            parameters.append(order_number)
        if reply_message_id is not None:
            clauses.append(
                "(p.prompt_message_id = ? OR p.admin_message_id = ?)"
            )
            parameters.extend([reply_message_id, reply_message_id])

        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT p.admin_id, p.order_number, p.admin_chat_id,
                       p.admin_message_id, p.prompt_message_id,
                       o.telegram_user_id,
                       o.telegram_username, o.product, o.price, o.game_id,
                       o.created_at, o.status, o.rejection_reason
                FROM pending_admin_rejections AS p
                JOIN orders AS o ON o.order_number = p.order_number
                WHERE {" AND ".join(clauses)}
                ORDER BY p.created_at DESC, p.pending_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return dict(row) if row is not None else None

    def reject_order(
        self, admin_id: int, order_number: int, reason: str
    ) -> Optional[dict[str, object]]:
        """Store the rejection reason and mark the selected order rejected."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.order_number, p.admin_chat_id, p.admin_message_id,
                       o.telegram_user_id, o.telegram_username, o.product,
                       o.price, o.game_id, o.created_at, o.status,
                       o.rejection_reason
                FROM pending_admin_rejections AS p
                JOIN orders AS o ON o.order_number = p.order_number
                WHERE p.admin_id = ?
                  AND p.order_number = ?
                """,
                (admin_id, order_number),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None

            updated = connection.execute(
                """
                UPDATE orders
                SET status = 'rejected',
                    rejection_reason = ?
                WHERE order_number = ?
                  AND status = 'pending'
                """,
                (reason, row["order_number"]),
            )
            if updated.rowcount != 1:
                return None

            connection.execute(
                """
                DELETE FROM pending_admin_rejections
                WHERE admin_id = ?
                  AND order_number = ?
                """,
                (admin_id, order_number),
            )
            rejected = dict(row)
            rejected["status"] = "rejected"
            rejected["rejection_reason"] = reason
            return rejected

    def cancel_checkout(self, telegram_user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM pending_checkouts
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            )


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
    buttons = [
        InlineKeyboardButton(
            f"{product_name} — {price}",
            callback_data=f"{SHOP_PRODUCT_CALLBACK_PREFIX}{product_id}",
        )
        for product_id, (product_name, price) in SHOP_PRODUCTS.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


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
                    "❌ إلغاء الطلب",
                    callback_data=SHOP_CANCEL_CALLBACK,
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


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def parse_game_id(text: str) -> Optional[str]:
    match = re.fullmatch(r"id:\s*([0-9]+)", text.strip(), re.IGNORECASE)
    return match.group(1) if match else None


def parse_admin_id(raw_admin_id: Optional[str]) -> int:
    if not raw_admin_id:
        raise RuntimeError(
            "ADMIN_ID is not configured. Add the Telegram admin ID as a Replit Secret."
        )

    try:
        admin_id = int(raw_admin_id)
    except ValueError as error:
        raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.") from error

    if admin_id <= 0:
        raise RuntimeError("ADMIN_ID must be a positive Telegram user ID.")
    return admin_id


def order_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ إلغاء الطلب",
                    callback_data=SHOP_CANCEL_CALLBACK,
                )
            ]
        ]
    )


def order_admin_keyboard(order_number: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تم الشحن",
                    callback_data=f"{ORDER_COMPLETE_CALLBACK_PREFIX}{order_number}",
                ),
                InlineKeyboardButton(
                    "❌ لم يتم الشحن",
                    callback_data=f"{ORDER_REJECT_CALLBACK_PREFIX}{order_number}",
                ),
            ]
        ]
    )


def parse_order_number_callback(
    callback_data: Optional[str], prefix: str
) -> Optional[int]:
    if not callback_data or not callback_data.startswith(prefix):
        return None

    raw_order_number = callback_data[len(prefix) :]
    try:
        order_number = int(raw_order_number)
    except ValueError:
        return None
    return order_number if order_number > 0 else None


def admin_order_status_text(
    original_text: str,
    status_text: str,
    reason: Optional[str] = None,
) -> str:
    updated_text = re.sub(
        r"\n(?:⏳ )?الحالة:.*?(?=\n|$)",
        "",
        original_text,
    ).rstrip()
    updated_text += f"\nالحالة: {status_text}"
    if reason is not None:
        updated_text += f"\nالسبب: {reason}"
    return updated_text


def order_payment_prompt(
    product_name: str, price: str
) -> str:
    return (
        f"🛒 {product_name}\n"
        f"💰 السعر: {price}\n\n"
        "⚠️ يلزمك تخلّص قبل إتمام الطلب.\n\n"
        "1️⃣ بعد الدفع، ابعث الـID متاعك بالشكل هذا:\n"
        "id: 123456789\n\n"
        "2️⃣ من بعد نطلبو منك معلومة الدفع باش نكملو الطلب."
    )


def order_admin_message(
    order_number: int,
    product: str,
    price: str,
    game_id: str,
    user_id: int,
    username: str,
    payment_info: str,
) -> str:
    return (
        "🛒 طلب شراء جديد\n"
        f"رقم الطلب: #{order_number}\n"
        f"المنتج: {product}\n"
        f"السعر: {price}\n"
        f"ID اللعبة: {game_id}\n"
        f"Telegram user ID: {user_id}\n"
        f"Username: {username}\n"
        f"معلومة الدفع: {payment_info}\n"
        "الحالة: ⏳ قيد التأكيد"
    )


def order_admin_result_message(
    order: dict[str, object],
    status_text: str,
    reason: Optional[str] = None,
) -> str:
    username = order.get("telegram_username")
    formatted_username = f"@{username}" if username else "غير متوفر"
    message = (
        "🛒 طلب شراء\n"
        f"رقم الطلب: #{int(order['order_number'])}\n"
        f"المنتج: {order['product']}\n"
        f"السعر: {order['price']}\n"
        f"ID اللعبة: {order['game_id']}\n"
        f"Telegram user ID: {int(order['telegram_user_id'])}\n"
        f"Username: {formatted_username}\n"
        f"الحالة: {status_text}"
    )
    if reason is not None:
        message += f"\nالسبب: {reason}"
    return message


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

    if not is_private_chat(update):
        await query.answer(
            "الشراء متاح في المحادثة الخاصة مع البوت فقط.",
            show_alert=True,
        )
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
    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    get_store(context).start_checkout(
        user_id,
        product_id,
        product_name,
        price,
    )
    await query.edit_message_text(
        order_payment_prompt(product_name, price),
        reply_markup=product_keyboard(product_id),
    )


async def shop_purchase_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    if not is_private_chat(update):
        await query.answer(
            "الشراء متاح في المحادثة الخاصة مع البوت فقط.",
            show_alert=True,
        )
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

    product_id, product_name, price = product
    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    get_store(context).start_checkout(
        user_id,
        product_id,
        product_name,
        price,
    )
    await query.edit_message_text(
        order_payment_prompt(product_name, price),
        reply_markup=order_cancel_keyboard(),
    )


async def shop_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    if not is_private_chat(update):
        await query.answer(
            "المتجر متاح في المحادثة الخاصة مع البوت فقط.",
            show_alert=True,
        )
        return

    await query.answer()
    await query.edit_message_text(
        "🛒 متجر YSF\nاختار المنتج اللي تحب عليه:",
        reply_markup=shop_keyboard(),
    )


async def shop_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    if not is_private_chat(update):
        await query.answer(
            "إلغاء الطلب متاح في المحادثة الخاصة مع البوت فقط.",
            show_alert=True,
        )
        return

    await query.answer()
    user_id = get_telegram_user_id(update)
    if user_id is not None:
        get_store(context).cancel_checkout(user_id)

    await query.edit_message_text(
        "تم إلغاء الطلب. تنجم ترجع للمتجر وقت اللي تحب.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ رجوع للمتجر",
                        callback_data=SHOP_MENU_CALLBACK,
                    )
                ]
            ]
        ),
    )


async def admin_order_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    admin_id = context.application.bot_data.get("admin_id")
    user_id = get_telegram_user_id(update)
    if (
        not is_private_chat(update)
        or not isinstance(admin_id, int)
        or user_id != admin_id
    ):
        await query.answer(
            "هذا الإجراء متاح للإدمن فقط.",
            show_alert=True,
        )
        return

    complete_order_number = parse_order_number_callback(
        query.data, ORDER_COMPLETE_CALLBACK_PREFIX
    )
    reject_order_number = parse_order_number_callback(
        query.data, ORDER_REJECT_CALLBACK_PREFIX
    )
    is_completion = complete_order_number is not None
    order_number = complete_order_number or reject_order_number
    if order_number is None:
        await query.answer("الطلب غير صالح.", show_alert=True)
        return

    store = get_store(context)
    if is_completion:
        order = store.complete_order(order_number)
        if order is None:
            await query.answer(
                "الطلب تمت معالجته مسبقًا.",
                show_alert=True,
            )
            return

        await query.answer("تم تحديث حالة الطلب.")
        customer_message = (
            "✅ تم شحن طلبك بنجاح.\n"
            "تنجم تدخل للعبة وتتأكد من وصول الجواهر/المنتج.\n"
            f"رقم الطلب: #{order_number}"
        )
        try:
            await context.bot.send_message(
                chat_id=int(order["telegram_user_id"]),
                text=customer_message,
            )
        except Exception:
            logger.exception(
                "Failed to notify customer for completed order #%s.",
                order_number,
            )

        if query.message is not None:
            original_text = query.message.text or "🛒 طلب شراء"
            updated_text = admin_order_status_text(
                original_text,
                "✅ تم الشحن",
            )
            try:
                await query.edit_message_text(
                    updated_text,
                    reply_markup=None,
                )
            except Exception:
                logger.exception(
                    "Failed to update admin message for order #%s.",
                    order_number,
                )
        return

    if query.message is None:
        await query.answer("تعذّر فتح الطلب.", show_alert=True)
        return

    order = store.begin_rejection(
        admin_id=admin_id,
        order_number=order_number,
        admin_chat_id=query.message.chat_id,
        admin_message_id=query.message.message_id,
    )
    if order is None:
        pending_rejection = store.get_pending_admin_rejection(
            admin_id,
            order_number=order_number,
        )
        if (
            pending_rejection is not None
        ):
            await query.answer(
                "اكتب سبب عدم الشحن في رسالة هنا.",
                show_alert=True,
            )
            return

        await query.answer(
            "الطلب تمت معالجته مسبقًا.",
            show_alert=True,
        )
        return

    await query.answer("في انتظار سبب عدم الشحن.")
    original_text = query.message.text or "🛒 طلب شراء"
    waiting_text = admin_order_status_text(
        original_text,
        "pending",
    ) + "\n✍️ في انتظار سبب عدم الشحن..."
    try:
        await query.edit_message_text(
            waiting_text,
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Failed to update admin message for rejected order #%s.",
            order_number,
        )

    prompt_message = await context.bot.send_message(
        chat_id=admin_id,
        text="✍️ اكتب الآن سبب عدم الشحن، وسيتم إرساله للزبون.",
        reply_to_message_id=query.message.message_id,
        reply_markup=ForceReply(selective=True),
    )
    if not store.set_rejection_prompt_message(
        admin_id,
        order_number,
        prompt_message.message_id,
    ):
        logger.error(
            "Failed to save rejection prompt for order #%s.",
            order_number,
        )


async def handle_admin_rejection_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if update.message is None or not is_private_chat(update):
        return False

    admin_id = context.application.bot_data.get("admin_id")
    user_id = get_telegram_user_id(update)
    if not isinstance(admin_id, int) or user_id != admin_id:
        return False

    store = get_store(context)
    reply_message = update.message.reply_to_message
    reply_message_id = (
        reply_message.message_id if reply_message is not None else None
    )
    pending = store.get_pending_admin_rejection(
        admin_id,
        reply_message_id=reply_message_id,
    )
    if pending is None:
        pending_rejections = store.get_pending_admin_rejections(admin_id)
        if len(pending_rejections) == 1:
            pending = pending_rejections[0]
        elif len(pending_rejections) > 1:
            await update.message.reply_text(
                "عندك أكثر من طلب يستنى سبب الرفض. "
                "استعمل Reply على رسالة السبب الخاصة بالطلب الصحيح."
            )
            return True

    if pending is None:
        return False

    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text(
            "اكتب سبب عدم الشحن في رسالة واضحة."
        )
        return True
    if len(reason) > MAX_REJECTION_REASON_LENGTH:
        await update.message.reply_text(
            f"السبب ما يفوتش {MAX_REJECTION_REASON_LENGTH} حرف."
        )
        return True

    order_number = int(pending["order_number"])
    order = store.reject_order(admin_id, order_number, reason)
    if order is None:
        await update.message.reply_text(
            "الطلب تمت معالجته مسبقًا."
        )
        return True

    order_number = int(order["order_number"])
    customer_message = (
        "❌ لم يتم شحن طلبك.\n"
        f"السبب: {reason}\n"
        f"رقم الطلب: #{order_number}"
    )
    try:
        await context.bot.send_message(
            chat_id=int(order["telegram_user_id"]),
            text=customer_message,
        )
    except Exception:
        logger.exception(
            "Failed to notify customer for rejected order #%s.",
            order_number,
        )

    updated_text = order_admin_result_message(
        order,
        "❌ لم يتم الشحن",
        reason,
    )
    try:
        await context.bot.edit_message_text(
            chat_id=int(pending["admin_chat_id"]),
            message_id=int(pending["admin_message_id"]),
            text=updated_text,
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Failed to update admin message for rejected order #%s.",
            order_number,
        )

    await update.message.reply_text(
        f"تم تسجيل سبب عدم الشحن للطلب #{order_number}."
    )
    return True


async def handle_order_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Collect the game ID and payment info in a private chat only."""
    if update.message is None or not is_private_chat(update):
        return

    if await handle_admin_rejection_message(update, context):
        return

    user_id = get_telegram_user_id(update)
    if user_id is None:
        return

    pending = get_store(context).get_pending_checkout(user_id)
    if pending is None:
        return

    text = update.message.text.strip()
    _, _, _, game_id = pending

    if game_id is None:
        parsed_game_id = parse_game_id(text)
        if parsed_game_id is None:
            await update.message.reply_text(
                "الصيغة غالطة.\n"
                "ابعث الـID بالشكل هذا:\n"
                "id: 123456789"
            )
            return

        if not get_store(context).set_checkout_game_id(
            user_id, parsed_game_id
        ):
            await update.message.reply_text(
                "تعذّر حفظ الـID. عاود المحاولة من المتجر."
            )
            return

        await update.message.reply_text(
            "مريقل، وصلني الـID متاعك.\n"
            "توا ابعث رمز/معلومة الدفع المطلوبة باش نكمّلوا الطلب."
        )
        return

    if not text or len(text) > MAX_PAYMENT_INFO_LENGTH:
        await update.message.reply_text(
            f"ابعث معلومة دفع واضحة وما تفوتش {MAX_PAYMENT_INFO_LENGTH} حرف."
        )
        return

    username = update.effective_user.username
    store = get_store(context)
    order = store.create_order(user_id, username)
    if order is None:
        await update.message.reply_text(
            "ما لقيناش طلب قاعد. عاود اختار المنتج من المتجر."
        )
        return

    order_number, stored_product, stored_price, stored_game_id = order
    admin_id = context.application.bot_data.get("admin_id")
    if not isinstance(admin_id, int):
        logger.error("ADMIN_ID is not configured; order #%s was created.", order_number)
        await update.message.reply_text(
            "تسجّل الطلب، أما صار مشكل في إعلام الأدمن. حاول تتصل بالإدارة."
        )
        return

    admin_username = f"@{username}" if username else "غير متوفر"
    admin_message = order_admin_message(
        order_number=order_number,
        product=stored_product,
        price=stored_price,
        game_id=stored_game_id,
        user_id=user_id,
        username=admin_username,
        payment_info=text,
    )

    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=admin_message,
            reply_markup=order_admin_keyboard(order_number),
        )
    except Exception:
        logger.exception(
            "Failed to notify admin for order #%s.", order_number
        )
        await update.message.reply_text(
            f"تسجّل طلبك #{order_number}، أما صار مشكل في إعلام الأدمن."
        )
        return

    await update.message.reply_text(
        "✅ تم تسجيل طلبك بنجاح!\n\n"
        f"📦 رقم الطلب: #{order_number}\n"
        f"🛒 المنتج: {stored_product}\n"
        f"💰 السعر: {stored_price}\n"
        f"🆔 ID اللعبة: {stored_game_id}\n\n"
        "الإدارة باش تراجع الطلب وتكمّل الشحن يدويًا."
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
    token: str,
    store: Optional[UserStore] = None,
    admin_id: Optional[int] = None,
) -> Application:
    """Create the bot application, storage, and command handlers."""
    application = (
        Application.builder()
        .token(token)
        .post_init(register_commands)
        .build()
    )
    application.bot_data["store"] = store or UserStore()
    application.bot_data["admin_id"] = admin_id
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("points", points))
    application.add_handler(CommandHandler("invite", invite))
    application.add_handler(CommandHandler("daily", daily))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & filters.Regex(f"^{re.escape(SHOP_MENU_BUTTON)}$"),
            show_shop,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_order_message,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin_order_callback,
            pattern=(
                f"^({re.escape(ORDER_COMPLETE_CALLBACK_PREFIX)}"
                f"|{re.escape(ORDER_REJECT_CALLBACK_PREFIX)})"
            ),
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
    application.add_handler(
        CallbackQueryHandler(
            shop_cancel_callback,
            pattern=f"^{re.escape(SHOP_CANCEL_CALLBACK)}$",
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

    admin_id = parse_admin_id(os.getenv("ADMIN_ID"))
    application = build_application(token, admin_id=admin_id)
    Thread(target=run_health_server, daemon=True).start()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()