from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
import re

STATUS = {
    "pending": "Ожидает оплаты", "awaiting_payment": "Оплата обрабатывается",
    "paid": "Оплачен · ждёт выдачи", "fulfilled": "Выдан",
    "refunded": "Возвращён", "cancelled": "Отменён", "expired": "Счёт истёк",
}


def db(context):
    return context.bot_data["db"]


def owner(update, context):
    return bool(update.effective_chat and update.effective_chat.type == "private"
                and update.effective_user
                and update.effective_user.id == context.bot_data["config"].owner_id)


async def require_owner(update, context):
    if owner(update, context):
        return True
    if update.callback_query:
        await update.callback_query.answer("Доступ только владельцу.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("🔒 Эта панель доступна только владельцу.")
    return False


def button(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def customer_contact(user_id, user=None):
    user = user or {}
    name = user.get('full_name') or 'Без имени'
    username = str(user.get('username') or '').lstrip('@')
    account = f"@{username}" if re.fullmatch(r'[A-Za-z0-9_]{1,32}', username) else 'username не указан'
    return f"Покупатель: {name}\nАккаунт: {account}\nTelegram ID: {user_id}"


def customer_actions(user_id, user=None):
    username = str((user or {}).get('username') or '').lstrip('@')
    url = f"https://t.me/{username}" if re.fullmatch(r'[A-Za-z0-9_]{1,32}', username) else f"tg://user?id={user_id}"
    return [[button('💬 Написать через бота', f's:write:{user_id}')],
            [InlineKeyboardButton('👤 Написать в личку', url=url)]]


def main_keyboard(is_owner=False):
    rows = [[button("🛍 Каталог", "catalog:0")],
            [button("📦 Мои заказы", "orders:0"), button("💬 Поддержка", "s:open")],
            [button("🎟 Ввести промокод", "promo")],
            [button("📄 Условия покупки", "terms")]]
    if is_owner:
        rows.append([button("⚙️ Панель владельца", "a:home")])
    return InlineKeyboardMarkup(rows)


def text_chunks(text, limit=3500):
    """Fit Telegram limits even when a message consists of non-BMP emoji."""
    chunks, part, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            chunks.append(''.join(part))
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        chunks.append(''.join(part))
    return chunks or [' ']


async def send_text(bot, chat_id, text, reply_markup=None):
    chunks = text_chunks(text)
    result = None
    for index, chunk in enumerate(chunks):
        result = await bot.send_message(chat_id=chat_id, text=chunk,
                                        reply_markup=reply_markup if index == len(chunks)-1 else None)
    return result


async def show(update, text, rows=None):
    markup = InlineKeyboardMarkup(rows) if rows is not None else None
    chunks = text_chunks(text)
    if len(chunks) > 1:
        result = None
        for index, chunk in enumerate(chunks):
            result = await update.effective_message.reply_text(chunk, reply_markup=markup if index == len(chunks)-1 else None)
        return result
    query = update.callback_query
    if query and query.message and getattr(query.message, "text", None):
        try:
            return await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return query.message
            # Old/removed message: send a new navigation card.
    if update.effective_message:
        return await update.effective_message.reply_text(text, reply_markup=markup)


async def remember(update, context):
    user = update.effective_user
    if user:
        db(context).upsert_user(user.id, user.username or "", user.full_name)


def order_text(order):
    return (f"📦 Заказ #{order['id']}\n\n{order['product_title']}\n"
            f"Количество: {order['quantity']}\n"
            f"Стоимость: {order['unit_price']} ⭐ × {order['quantity']} = {order['total']} ⭐\n"
            f"Статус: {STATUS.get(order['status'], order['status'])}")


def pagination(prefix, offset, more, size=8):
    row = []
    if offset:
        row.append(button("‹ Назад", f"{prefix}:{max(0, offset-size)}"))
    if more:
        row.append(button("Далее ›", f"{prefix}:{offset+size}"))
    return [row] if row else []
