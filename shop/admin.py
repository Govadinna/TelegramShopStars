"""Owner-only, Russian-language administration inside Telegram."""

from __future__ import annotations

import logging
import re
from typing import Any

from telegram.error import BadRequest, TelegramError

from .common import button, customer_actions, db, order_text, require_owner, show
from .store import MAX_PROMO_ACTIVATIONS, StoreError, normalize_promo_code

log = logging.getLogger(__name__)
PAGE_SIZE = 8
SETTING_LABELS = {
    "shop_name": "Название магазина",
    "welcome_text": "Приветствие",
    "after_purchase_text": "Текст после оплаты",
    "manager_username": "Контакт менеджера",
    "terms": "Условия покупки",
}
FIELD_LABELS = {
    "title": "Название",
    "description": "Описание",
    "price": "Цена в звёздах",
    "stock": "Остаток",
    "photo_id": "Фото",
}


def _short(value: Any, limit: int = 48) -> str:
    text = str(value or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _integer(value: str, minimum: int, maximum: int, label: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError(f"{label}: введите целое число от {minimum} до {maximum}.")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{label}: введите целое число от {minimum} до {maximum}.")
    return result


def _text(value: str, maximum: int, label: str, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{label} не может быть пустым.")
    if len(value) > maximum:
        raise ValueError(f"{label}: максимум {maximum} символов.")
    return value


def _stock(value: str) -> int | None:
    if value.strip() == "-":
        return None
    return _integer(value, 0, 1_000_000, "Остаток")


def _setting(key: str, value: str) -> str:
    if key not in SETTING_LABELS:
        raise ValueError("Неизвестная настройка.")
    if key == "manager_username":
        value = value.strip().removeprefix("@")
        if value == "-":
            return ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("Пришлите @username: 5–32 латинские буквы, цифры или подчёркивание, первая — буква. Для удаления пришлите -.")
        return value
    return _text(value, 100 if key == "shop_name" else 3000, SETTING_LABELS[key])


def _back(data: str = "a:home") -> list[list]:
    return [[button("← Назад", data)]]


def _pager(prefix: str, offset: int, has_next: bool) -> list:
    row = []
    if offset:
        row.append(button("← Предыдущие", f"{prefix}:{max(0, offset - PAGE_SIZE)}"))
    if has_next:
        row.append(button("Следующие →", f"{prefix}:{offset + PAGE_SIZE}"))
    return row


def _flow(context, kind: str, **values) -> None:
    context.user_data["flow"] = {"kind": "admin_" + kind, **values}


async def _home(update, context) -> None:
    await show(update, "⚙️ Панель владельца\nУправление доступно только вашему Telegram ID.", [
        [button("📁 Категории", "a:categories:0"), button("🛍 Товары", "a:products:0")],
        [button("📦 Заказы", "a:orders:0"), button("💬 Клиенты", "a:users:0")],
        [button("📊 Статистика", "a:stats"), button("✏️ Настройки", "a:settings")],
        [button("🎟 Промокоды", "a:promos:0")],
        [button("🏠 Магазин", "home")],
    ])


async def admin_command(update, context) -> None:
    if not await require_owner(update, context):
        return
    context.user_data.pop("flow", None)
    await _home(update, context)


def _promo(context, promo_id: int) -> dict:
    promo = db(context).get_promo(promo_id)
    if not promo:
        raise StoreError("Промокод удалён или не найден.")
    return promo


def _promo_limit(value: str) -> int | None:
    number = _integer(value, 0, MAX_PROMO_ACTIVATIONS, "Лимит активаций")
    return number or None


async def _promos(update, context, offset: int = 0) -> None:
    promos = db(context).list_promos(limit=PAGE_SIZE + 1, offset=offset)
    rows = [[button(
        f"{'🟢' if p['active'] else '⚪'} {_short(p['code'], 32)} · {p['activation_count']}",
        f"a:promo:{p['id']}",
    )] for p in promos[:PAGE_SIZE]]
    page = _pager("a:promos", offset, len(promos) > PAGE_SIZE)
    if page:
        rows.append(page)
    rows += [[button("➕ Создать промокод", "a:promo_add")], [button("← Панель", "a:home")]]
    await show(update, "🎟 Промокоды\n\nПромокоды только учитывают активации: скидок и бонусов нет.\n"
               + ("Выберите код для управления." if promos else "Промокодов на этой странице нет."), rows)


async def _promo_card(update, context, promo_id: int) -> None:
    promo = _promo(context, promo_id)
    limit = promo['activation_limit']
    status = "отключён" if not promo['active'] else (
        "лимит исчерпан" if promo['activation_count'] >= (limit or MAX_PROMO_ACTIVATIONS) else "включён")
    text = (f"🎟 Промокод: {promo['code']}\n\nСтатус: {status}\n"
            f"Счётчик активаций: {promo['activation_count']}\n"
            f"Клиентов в истории активаций: {promo['unique_activations']}\n"
            f"Лимит активаций: {limit if limit is not None else 'без ограничений'}\n\n"
            "Один клиент может активировать код один раз. Промокод не даёт скидок и бонусов.")
    rows = [
        [button("✏️ Изменить код", f"a:promo_rename:{promo_id}")],
        [button("🔢 Изменить счётчик", f"a:promo_count:{promo_id}")],
        [button("🎯 Изменить лимит", f"a:promo_limit:{promo_id}")],
        [button("⏸ Отключить" if promo['active'] else "▶️ Включить", f"a:promo_toggle:{promo_id}")],
        [button("🗑 Удалить", f"a:promo_delete:{promo_id}")],
        [button("← Промокоды", "a:promos:0")],
    ]
    await show(update, text, rows)


async def _categories(update, context, offset: int = 0) -> None:
    categories = db(context).list_categories(include_hidden=True)
    visible = categories[offset:offset + PAGE_SIZE]
    rows = [[button(("🟢 " if c["active"] else "⚪️ ") + _short(c["name"]), f"a:category:{c['id']}")] for c in visible]
    page = _pager("a:categories", offset, len(categories) > offset + PAGE_SIZE)
    if page:
        rows.append(page)
    rows += [[button("＋ Добавить категорию", "a:category_add")], [button("← Панель", "a:home")]]
    await show(update, "📁 Категории\n🟢 Видна · ⚪️ Скрыта\nСкрытая категория скрывает и её товары." if visible else "Категорий на этой странице нет.", rows)


def _category(context, category_id: int) -> dict:
    result = db(context).get_category(category_id)
    if not result or result.get("deleted"):
        raise StoreError("Категория не найдена или удалена.")
    return result


def _product(context, product_id: int) -> dict:
    result = db(context).get_product(product_id)
    if not result or result.get("deleted"):
        raise StoreError("Товар не найден или удалён.")
    return result


async def _category_card(update, context, category_id: int) -> None:
    category = _category(context, category_id)
    await show(update, f"📁 {category['name']}\nСтатус: {'видна' if category['active'] else 'скрыта'}", [
        [button("✏️ Переименовать", f"a:category_rename:{category_id}")],
        [button("Скрыть" if category["active"] else "Показать", f"a:category_toggle:{category_id}")],
        [button("＋ Добавить товар", f"a:product_add:{category_id}")],
        [button("🗑 Удалить", f"a:category_delete:{category_id}")],
        [button("← Категории", "a:categories:0")],
    ])


async def _products(update, context, offset: int = 0) -> None:
    products = db(context).list_products(include_hidden=True, limit=PAGE_SIZE + 1, offset=offset)
    rows = [[button(("🟢 " if p["active"] else "⚪️ ") + _short(p["title"], 36) + f" · {p['price']} ⭐", f"a:product:{p['id']}")] for p in products[:PAGE_SIZE]]
    page = _pager("a:products", offset, len(products) > PAGE_SIZE)
    if page:
        rows.append(page)
    rows += [[button("＋ Добавить товар", "a:product_choose:0")], [button("← Панель", "a:home")]]
    await show(update, "🛍 Товары\n🟢 Включён · ⚪️ Скрыт. Категория тоже должна быть видна покупателям." if products else "Товаров на этой странице нет.", rows)


async def _choose_category(update, context, offset: int = 0, product_id: int | None = None) -> None:
    categories = db(context).list_categories(include_hidden=True)
    prefix = "a:product_choose" if product_id is None else f"a:move_choose:{product_id}"
    rows = []
    for category in categories[offset:offset + PAGE_SIZE]:
        data = f"a:product_add:{category['id']}" if product_id is None else f"a:product_move:{product_id}:{category['id']}"
        rows.append([button(_short(category["name"]) + (" (скрыта)" if not category["active"] else ""), data)])
    page = _pager(prefix, offset, len(categories) > offset + PAGE_SIZE)
    if page:
        rows.append(page)
    if not categories:
        rows.append([button("＋ Создать категорию", "a:category_add")])
    rows.append([button("← Назад", "a:products:0" if product_id is None else f"a:product:{product_id}")])
    await show(update, "Выберите категорию для товара:" if categories else "Сначала создайте категорию.", rows)


async def _product_card(update, context, product_id: int) -> None:
    product = _product(context, product_id)
    category = db(context).get_category(product["category_id"])
    stock = "без ограничения" if product["stock"] is None else str(product["stock"])
    text = (f"🛍 {product['title']}\n\n{product['description']}\n\n"
            f"Цена: {product['price']} ⭐\nДоступный остаток: {stock}\n"
            f"Категория: {category['name'] if category else 'удалена'}\n"
            f"Статус: {'включён' if product['active'] else 'скрыт'}\n"
            f"Фото: {'есть' if product.get('photo_id') else 'нет'}\nID товара: {product_id}")
    if category and not category["active"]:
        text += "\nКатегория скрыта — покупатели товар не видят."
    await show(update, text, [
        [button("Название", f"a:edit:{product_id}:title"), button("Описание", f"a:edit:{product_id}:description")],
        [button("Цена", f"a:edit:{product_id}:price"), button("Остаток", f"a:edit:{product_id}:stock"), button("Фото", f"a:edit:{product_id}:photo_id")],
        [button("Перенести в категорию", f"a:move_choose:{product_id}:0")],
        [button("Скрыть" if product["active"] else "Показать", f"a:product_toggle:{product_id}")],
        [button("🗑 Удалить", f"a:product_delete:{product_id}")],
        [button("← Товары", "a:products:0")],
    ])


async def _orders(update, context, offset: int = 0) -> None:
    orders = db(context).list_orders(limit=PAGE_SIZE + 1, offset=offset)
    labels = {"pending": "счёт", "awaiting_payment": "ждём оплату", "paid": "оплачен", "fulfilled": "выдан", "refunded": "возврат", "cancelled": "отменён", "expired": "истёк"}
    rows = [[button(f"#{o['id']} · {labels.get(o['status'], o['status'])} · {o['total']} ⭐", f"a:order:{o['id']}")] for o in orders[:PAGE_SIZE]]
    page = _pager("a:orders", offset, len(orders) > PAGE_SIZE)
    if page:
        rows.append(page)
    rows.append([button("← Панель", "a:home")])
    await show(update, "📦 Заказы — сначала новые" if orders else "Заказов на этой странице нет.", rows)


def _order(context, order_id: int) -> dict:
    result = db(context).get_order(order_id)
    if not result:
        raise StoreError("Заказ не найден.")
    return result


async def _order_card(update, context, order_id: int) -> None:
    order = _order(context, order_id)
    user = db(context).get_user(order["user_id"])
    who = f"{user.get('full_name') or 'Без имени'}" if user else "Клиент"
    if user and user.get("username"):
        who += f" (@{user['username']})"
    text = order_text(order) + f"\n\nПокупатель: {who}\nTelegram ID: {order['user_id']}"
    if order["status"] == "awaiting_payment":
        text += "\nТовар зарезервирован. Ждём подтверждение Telegram; вручную снимать этот резерв нельзя."
    if order.get("inventory_issue"):
        text += "\n⚠️ Оплата получена, но при её обработке возникла проблема с остатком. Свяжитесь с клиентом."
    rows = customer_actions(order['user_id'], user)
    if order["status"] == "paid":
        rows.append([button("✅ Отметить выданным", f"a:fulfill:{order_id}")])
    if order["status"] in {"paid", "fulfilled"}:
        rows.append([button("↩️ Вернуть звёзды", f"a:refund:{order_id}")])
    rows.append([button("← Заказы", "a:orders:0")])
    await show(update, text, rows)


async def _users(update, context, offset: int = 0) -> None:
    users = db(context).list_users(limit=PAGE_SIZE + 1, offset=offset)
    rows = []
    for user in users[:PAGE_SIZE]:
        title = user.get("full_name") or ("@" + user["username"] if user.get("username") else str(user["id"]))
        rows.append([button(_short(title, 45) + f" · {user['id']}", f"a:user:{user['id']}")])
    page = _pager("a:users", offset, len(users) > PAGE_SIZE)
    if page:
        rows.append(page)
    rows.append([button("← Панель", "a:home")])
    await show(update, "💬 Клиенты\nНаписать можно пользователям, которые уже запускали бота." if users else "Клиентов на этой странице нет.", rows)


async def _user_card(update, context, user_id: int) -> None:
    user = db(context).get_user(user_id)
    if not user:
        raise StoreError("Клиент не найден.")
    recent = db(context).list_orders(user_id=user_id, limit=5)
    text = f"💬 {user.get('full_name') or 'Без имени'}\nTelegram ID: {user_id}\nUsername: {'@' + user['username'] if user.get('username') else 'не указан'}"
    rows = customer_actions(user_id, user)
    if recent:
        text += "\n\nПоследние заказы:"
        rows.extend([[button(f"Заказ #{o['id']} · {o['total']} ⭐", f"a:order:{o['id']}")] for o in recent])
    rows.append([button("← Клиенты", "a:users:0")])
    await show(update, text, rows)


async def _settings(update, context) -> None:
    rows = [[button(label, f"a:setting:{key}")] for key, label in SETTING_LABELS.items()]
    rows.append([button("← Панель", "a:home")])
    await show(update, "✏️ Настройки магазина\nКонтакт менеджера не даёт доступа к панели. Владелец задаётся только OWNER_ID в настройках запуска.", rows)


async def _start_product(update, context, category_id: int) -> None:
    _category(context, category_id)
    _flow(context, "product_add", step="title", values={"category_id": category_id})
    await show(update, "Новый товар · 1/6\nПришлите название (до 100 символов).\n\nОтмена: /cancel", _back("a:products:0"))


async def _edit_prompt(update, context, product_id: int, field: str) -> None:
    _product(context, product_id)
    if field not in FIELD_LABELS:
        raise ValueError("Неизвестное поле товара.")
    _flow(context, "product_edit", product_id=product_id, field=field)
    prompts = {
        "title": "Пришлите новое название (до 100 символов).",
        "description": "Пришлите новое описание (до 2500 символов). Для пустого описания пришлите -.",
        "price": "Пришлите цену целым числом от 1 до 1000000 ⭐.",
        "stock": "Пришлите доступный остаток от 0 до 1000000. Для неограниченного остатка — -.\nУже зарезервированные единицы в это число не входят.",
        "photo_id": "Отправьте новое фото как фотографию. Для удаления фото пришлите -.",
    }
    await show(update, prompts[field] + "\n\nОтмена: /cancel", _back(f"a:product:{product_id}"))


async def _refund(update, context, order_id: int) -> None:
    order = _order(context, order_id)
    if order["status"] == "refunded":
        await show(update, "Этот заказ уже возвращён.", _back(f"a:order:{order_id}"))
        return
    if order["status"] not in {"paid", "fulfilled"} or not order.get("charge_id"):
        raise StoreError("Возврат доступен только для оплаченного заказа с номером платежа Telegram.")
    try:
        result = await context.bot.refund_star_payment(user_id=order["user_id"], telegram_payment_charge_id=order["charge_id"])
        if not result:
            raise TelegramError("Telegram не подтвердил возврат")
    except BadRequest as exc:
        # Telegram may have completed a previous request whose response was lost.
        # Its explicit already-refunded response is safe to reconcile locally.
        if "charge_already_refunded" not in str(exc).lower():
            raise
    db(context).mark_refunded(order_id, order["charge_id"])
    try:
        await context.bot.send_message(chat_id=order["user_id"], text=f"По заказу #{order_id} оформлен полный возврат: {order['total']} ⭐. Звёзды возвращает Telegram.")
    except TelegramError:
        log.warning("Refund notification was not delivered for order %s", order_id)
    await show(update, f"Возврат #{order_id} подтверждён: {order['total']} ⭐.\nОстаток товара не увеличен автоматически.", _back(f"a:order:{order_id}"))


async def admin_callback(update, context) -> None:
    if not await require_owner(update, context):
        return
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        pass
    raw = query.data or ""
    if not isinstance(raw, str) or not raw.startswith("a:") or len(raw) > 64:
        await show(update, "Кнопка устарела. Откройте /admin.")
        return
    parts = raw.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    args = parts[2:]
    context.user_data.pop("flow", None)

    def numbers(count: int) -> list[int]:
        if len(args) != count or any(not re.fullmatch(r"[0-9]{1,16}", item) for item in args):
            raise ValueError("Кнопка устарела. Откройте /admin.")
        values = [int(item) for item in args]
        if any(value > 2**53 for value in values):
            raise ValueError("Некорректный номер.")
        return values

    try:
        if action == "home" and not args:
            await _home(update, context)
        elif action in {"categories", "products", "orders", "users", "product_choose", "promos"}:
            offset, = numbers(1)
            handlers = {"categories": _categories, "products": _products, "orders": _orders, "users": _users, "product_choose": _choose_category, "promos": _promos}
            await handlers[action](update, context, offset)
        elif action == "promo":
            promo_id, = numbers(1)
            await _promo_card(update, context, promo_id)
        elif action == "promo_add" and not args:
            _flow(context, "promo_add", step="code")
            await show(update, "Новый промокод · 1/2\nПришлите код: от 1 до 64 латинских или русских букв, цифр, _ или -.\n"
                       "Без пробелов. Регистр букв не важен.\n\nОтмена: /cancel", _back("a:promos:0"))
        elif action in {"promo_rename", "promo_count", "promo_limit"}:
            promo_id, = numbers(1)
            _promo(context, promo_id)
            _flow(context, action, promo_id=promo_id)
            prompts = {
                "promo_rename": "Пришлите новый код: 1–64 латинских или русских букв, цифр, _ или -. Без пробелов. Регистр не важен.",
                "promo_count": f"Пришлите новый счётчик от 0 до {MAX_PROMO_ACTIVATIONS}.\n"
                               "Изменение счётчика не удаляет историю: те же клиенты не смогут активировать код повторно.\n"
                               "Доступность активаций определяется этим счётчиком и лимитом.",
                "promo_limit": f"Пришлите лимит активаций от 1 до {MAX_PROMO_ACTIVATIONS}.\nДля активаций без ограничения пришлите 0.\n"
                               "Если счётчик достигнет лимита, новые активации будут недоступны.",
            }
            await show(update, prompts[action] + "\n\nОтмена: /cancel", _back(f"a:promo:{promo_id}"))
        elif action == "promo_toggle":
            promo_id, = numbers(1)
            promo = _promo(context, promo_id)
            db(context).update_promo(promo_id, active=not promo['active'])
            await _promo_card(update, context, promo_id)
        elif action in {"promo_delete", "promo_delete_confirm"}:
            promo_id, = numbers(1)
            promo = _promo(context, promo_id)
            if action == "promo_delete":
                await show(update, f"Удалить промокод «{promo['code']}» и историю его активаций?\n"
                           "Если создать этот код заново, клиенты смогут активировать его снова.",
                           [[button("Да, удалить", f"a:promo_delete_confirm:{promo_id}")],
                            [button("Отмена", f"a:promo:{promo_id}")]])
            else:
                db(context).delete_promo(promo_id)
                await _promos(update, context)
        elif action == "category":
            category_id, = numbers(1)
            await _category_card(update, context, category_id)
        elif action == "category_add" and not args:
            _flow(context, "category_add")
            await show(update, "Пришлите название категории (до 100 символов).\n\nОтмена: /cancel", _back("a:categories:0"))
        elif action == "category_rename":
            category_id, = numbers(1)
            _category(context, category_id)
            _flow(context, "category_rename", category_id=category_id)
            await show(update, "Пришлите новое название категории (до 100 символов).\n\nОтмена: /cancel", _back(f"a:category:{category_id}"))
        elif action == "category_toggle":
            category_id, = numbers(1)
            category = _category(context, category_id)
            db(context).update_category(category_id, active=not category["active"])
            await _category_card(update, context, category_id)
        elif action in {"category_delete", "category_delete_confirm"}:
            category_id, = numbers(1)
            category = _category(context, category_id)
            if action == "category_delete":
                await show(update, f"Удалить категорию «{category['name']}» и все её товары?\nИстория заказов сохранится. Восстановить каталог этой кнопкой нельзя.", [[button("Да, удалить категорию и товары", f"a:category_delete_confirm:{category_id}")], [button("Отмена", f"a:category:{category_id}")]])
            else:
                db(context).delete_category(category_id)
                await _categories(update, context)
        elif action == "product":
            product_id, = numbers(1)
            await _product_card(update, context, product_id)
        elif action == "product_add":
            category_id, = numbers(1)
            await _start_product(update, context, category_id)
        elif action == "edit":
            if len(args) != 2 or not re.fullmatch(r"[0-9]{1,16}", args[0]):
                raise ValueError("Кнопка устарела. Откройте /admin.")
            await _edit_prompt(update, context, int(args[0]), args[1])
        elif action == "move_choose":
            product_id, offset = numbers(2)
            _product(context, product_id)
            await _choose_category(update, context, offset, product_id)
        elif action == "product_move":
            product_id, category_id = numbers(2)
            _product(context, product_id)
            _category(context, category_id)
            db(context).update_product(product_id, category_id=category_id)
            await _product_card(update, context, product_id)
        elif action == "product_toggle":
            product_id, = numbers(1)
            product = _product(context, product_id)
            db(context).update_product(product_id, active=not product["active"])
            await _product_card(update, context, product_id)
        elif action in {"product_delete", "product_delete_confirm"}:
            product_id, = numbers(1)
            product = _product(context, product_id)
            if action == "product_delete":
                await show(update, f"Удалить товар «{product['title']}»?\nИстория заказов сохранится. Восстановить товар этой кнопкой нельзя.", [[button("Да, удалить товар", f"a:product_delete_confirm:{product_id}")], [button("Отмена", f"a:product:{product_id}")]])
            else:
                db(context).delete_product(product_id)
                await _products(update, context)
        elif action == "order":
            order_id, = numbers(1)
            await _order_card(update, context, order_id)
        elif action in {"fulfill", "fulfill_confirm"}:
            order_id, = numbers(1)
            order = _order(context, order_id)
            if order["status"] != "paid":
                raise StoreError("Отметить выданным можно только оплаченный заказ.")
            if action == "fulfill":
                await show(update, f"Товар по заказу #{order_id} уже отправлен покупателю?\nЭта кнопка меняет статус заказа. Сам товар нужно отправить через «Написать покупателю».", [[button("Да, товар выдан", f"a:fulfill_confirm:{order_id}")], [button("Отмена", f"a:order:{order_id}")]])
            else:
                db(context).mark_fulfilled(order_id)
                await _order_card(update, context, order_id)
        elif action == "refund":
            order_id, = numbers(1)
            order = _order(context, order_id)
            if order["status"] not in {"paid", "fulfilled"} or not order.get("charge_id"):
                raise StoreError("Возврат доступен только для оплаченного заказа.")
            await show(update, f"Вернуть покупателю полную сумму заказа #{order_id}: {order['total']} ⭐?\nTelegram спишет звёзды с баланса бота. Возврат нельзя отменить. Остаток товара не изменится.", [[button(f"Подтверждаю возврат {order['total']} ⭐", f"a:refund_confirm:{order_id}")], [button("Отмена", f"a:order:{order_id}")]])
        elif action == "refund_confirm":
            order_id, = numbers(1)
            await _refund(update, context, order_id)
        elif action == "user":
            user_id, = numbers(1)
            await _user_card(update, context, user_id)
        elif action == "settings" and not args:
            await _settings(update, context)
        elif action == "setting" and len(args) == 1 and args[0] in SETTING_LABELS:
            key = args[0]
            _flow(context, "setting", key=key)
            current = db(context).get_setting(key)
            suffix = "\nОтправьте @username или - для удаления. Менеджер не получает доступ к админке." if key == "manager_username" else f"\nПришлите новый текст (до {100 if key == 'shop_name' else 3000} символов)."
            await show(update, f"{SETTING_LABELS[key]}\n\nСейчас:\n{current or 'не задано'}{suffix}\n\nОтмена: /cancel", _back("a:settings"))
        elif action == "stats" and not args:
            stats = db(context).stats()
            await show(update, f"📊 Статистика\n\nКлиентов: {stats['users']}\nТоваров: {stats['products']}\nОплаченных и выданных заказов: {stats['paid_orders']}\nСумма этих заказов: {stats['revenue']} ⭐\nЖдут подтверждения оплаты: {stats['pending_orders']}\n\nВозвращённые заказы не входят в сумму. Это учёт заказов, а не доступный баланс Telegram.", _back())
        else:
            await show(update, "Кнопка устарела. Откройте /admin.", _back())
    except (ValueError, StoreError) as exc:
        await show(update, str(exc), _back())
    except TelegramError:
        log.warning("Telegram admin operation failed: %s", action)
        text = "Telegram не подтвердил операцию. Попробуйте открыть раздел заново."
        if action == "refund_confirm":
            text = "Telegram не подтвердил возврат. Если запрос прервался, сначала проверьте историю платежей бота. Повторный запрос того же платежа не создаст второй возврат."
        await show(update, text, _back())


def _photo(message) -> str | None:
    photos = getattr(message, "photo", None)
    if photos:
        return photos[-1].file_id
    if (getattr(message, "text", None) or "").strip() == "-":
        return None
    raise ValueError("Отправьте изображение как фотографию (не файл) или - без фото.")


async def _product_add_input(update, context, flow: dict) -> None:
    message = update.effective_message
    value = getattr(message, "text", None) or ""
    step = flow["step"]
    values = flow["values"]
    prompts = {
        "description": "Новый товар · 2/6\nПришлите описание (до 2500 символов) или - для пустого описания.",
        "price": "Новый товар · 3/6\nПришлите цену целым числом от 1 до 1000000 ⭐.",
        "stock": "Новый товар · 4/6\nПришлите остаток от 0 до 1000000 или - для неограниченного количества.",
        "photo_id": "Новый товар · 5/6\nОтправьте фото как фотографию или - без фото.",
    }
    if step == "title":
        values["title"] = _text(value, 100, "Название")
        flow["step"] = "description"
    elif step == "description":
        values["description"] = "" if value.strip() == "-" else _text(value, 2500, "Описание")
        flow["step"] = "price"
    elif step == "price":
        values["price"] = _integer(value, 1, 1_000_000, "Цена")
        flow["step"] = "stock"
    elif step == "stock":
        values["stock"] = _stock(value)
        flow["step"] = "photo_id"
    elif step == "photo_id":
        values["photo_id"] = _photo(message)
        # One transaction creates a complete product. Earlier wizard steps are
        # transient, so cancelling or restarting never publishes a half product.
        product_id = db(context).add_product(**values)
        context.user_data.pop("flow", None)
        await show(update, "✅ Новый товар · 6/6\nТовар сохранён. Он виден покупателям, если категория включена.")
        await _product_card(update, context, product_id)
        return
    else:
        context.user_data.pop("flow", None)
        raise ValueError("Сценарий устарел. Начните добавление товара заново в /admin.")
    await show(update, prompts[flow["step"]] + "\n\nОтмена: /cancel", _back("a:products:0"))


async def admin_input(update, context) -> bool:
    flow = context.user_data.get("flow")
    if not isinstance(flow, dict) or not str(flow.get("kind", "")).startswith("admin_"):
        return False
    if not await require_owner(update, context):
        return True
    if update.effective_message is None:
        return True
    value = getattr(update.effective_message, "text", None) or ""
    kind = flow["kind"]
    try:
        if kind == "admin_promo_add":
            if flow['step'] == 'code':
                flow['code'] = normalize_promo_code(value)
                flow['step'] = 'limit'
                await show(update, f"Новый промокод · 2/2\nКод: {flow['code']}\n\n"
                           f"Пришлите лимит активаций от 1 до {MAX_PROMO_ACTIVATIONS}.\n"
                           "Для активаций без ограничения пришлите 0.\n\nОтмена: /cancel", _back("a:promos:0"))
            elif flow['step'] == 'limit':
                limit = _promo_limit(value)
                try:
                    promo_id = db(context).add_promo(flow['code'], limit)
                except StoreError:
                    flow['step'] = 'code'
                    flow.pop('code', None)
                    raise
                context.user_data.pop('flow', None)
                await _promo_card(update, context, promo_id)
            else:
                raise StoreError("Сценарий устарел. Начните создание промокода заново в /admin.")
        elif kind in {"admin_promo_rename", "admin_promo_count", "admin_promo_limit"}:
            promo_id = flow['promo_id']
            _promo(context, promo_id)
            if kind == 'admin_promo_rename':
                fields = {'code': normalize_promo_code(value)}
            elif kind == 'admin_promo_count':
                fields = {'activation_count': _integer(value, 0, MAX_PROMO_ACTIVATIONS, "Счётчик активаций")}
            else:
                fields = {'activation_limit': _promo_limit(value)}
            db(context).update_promo(promo_id, **fields)
            context.user_data.pop('flow', None)
            await _promo_card(update, context, promo_id)
        elif kind == "admin_category_add":
            name = _text(value, 100, "Название категории")
            category_id = db(context).add_category(name)
            context.user_data.pop("flow", None)
            await _category_card(update, context, category_id)
        elif kind == "admin_category_rename":
            name = _text(value, 100, "Название категории")
            _category(context, flow["category_id"])
            db(context).update_category(flow["category_id"], name=name)
            context.user_data.pop("flow", None)
            await _category_card(update, context, flow["category_id"])
        elif kind == "admin_setting":
            new_value = _setting(flow["key"], value)
            db(context).set_setting(flow["key"], new_value)
            context.user_data.pop("flow", None)
            await show(update, "✅ Настройка сохранена.", [[button("← Настройки", "a:settings")]])
        elif kind == "admin_product_add":
            await _product_add_input(update, context, flow)
        elif kind == "admin_product_edit":
            product_id, field = flow["product_id"], flow["field"]
            _product(context, product_id)
            if field == "photo_id":
                new_value = _photo(update.effective_message)
            elif field == "price":
                new_value = _integer(value, 1, 1_000_000, "Цена")
            elif field == "stock":
                new_value = _stock(value)
            elif field == "title":
                new_value = _text(value, 100, "Название")
            elif field == "description":
                new_value = "" if value.strip() == "-" else _text(value, 2500, "Описание")
            else:
                raise ValueError("Неизвестное поле товара.")
            db(context).update_product(product_id, **{field: new_value})
            context.user_data.pop("flow", None)
            await _product_card(update, context, product_id)
        else:
            context.user_data.pop("flow", None)
            await show(update, "Сценарий устарел. Откройте /admin.")
    except (ValueError, StoreError) as exc:
        await show(update, f"{exc}\n\nПопробуйте ещё раз или /cancel.")
    return True
