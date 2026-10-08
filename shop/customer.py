"""Customer catalogue and Telegram Stars purchase flow."""
import asyncio
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.error import BadRequest, TelegramError

from .common import button, customer_actions, customer_contact, db, main_keyboard, order_text, owner, pagination, remember, send_text, show
from .store import StoreError

log = logging.getLogger(__name__)
PAGE = 8


async def start(update, context):
    context.user_data.clear()
    await remember(update, context)
    store = db(context)
    test_label = "🧪 ТЕСТОВАЯ СРЕДА TELEGRAM\n\n" if context.bot_data['config'].test_environment else ""
    await show(update, f"{test_label}{store.get_setting('shop_name')}\n\n"
               f"{store.get_setting('welcome_text')}\n\n"
               "Оплата — Telegram Stars ⭐\nВыдача товара — владельцем через этот бот.",
               main_keyboard(owner(update, context)).inline_keyboard)


async def terms(update, context):
    await show(update, "📄 Условия покупки\n\n" + db(context).get_setting("terms"),
               [[button("💬 Уточнить у владельца", "s:open")], [button("🏠 Главное меню", "home")]])


def available(store, product_id):
    item = store.get_product(product_id)
    if not item or not item['active'] or item.get('deleted'):
        raise StoreError("Этот товар сейчас недоступен.")
    category = store.get_category(item['category_id'])
    if not category or not category['active'] or category.get('deleted'):
        raise StoreError("Эта категория сейчас недоступна.")
    return item


def validate_quantity(item, quantity, context):
    if not 1 <= quantity <= context.bot_data['config'].max_quantity:
        raise StoreError(f"Введите количество от 1 до {context.bot_data['config'].max_quantity}.")
    if item['stock'] is not None and quantity > item['stock']:
        raise StoreError(f"Сейчас доступно: {item['stock']} шт.")
    if item['price'] * quantity > 1_000_000:
        raise StoreError("Сумма одного заказа не должна превышать 1 000 000 ⭐.")


async def catalogue(update, context, offset=0):
    categories = db(context).list_categories()
    page = categories[offset:offset+PAGE]
    rows = [[button(f"📁 {c['name']}", f"cat:{c['id']}:0")] for c in page]
    rows += pagination("catalog", offset, len(categories) > offset+PAGE, PAGE)
    rows.append([button("🏠 Главное меню", "home")])
    text = "🛍 Каталог\n\nВыберите категорию:" if categories else "🛍 Каталог пока пуст. Скоро здесь появятся товары."
    await show(update, text, rows)


async def category_page(update, context, category_id, offset):
    store = db(context)
    category = store.get_category(category_id)
    if not category or not category['active'] or category.get('deleted'):
        raise StoreError("Категория недоступна.")
    items = store.list_products(category_id, limit=PAGE+1, offset=offset)
    rows = [[button(f"{'⚪' if p['stock'] == 0 else '🎮'} {p['title'][:45]} · {p['price']} ⭐", f"p:{p['id']}")]
            for p in items[:PAGE]]
    rows += pagination(f"cat:{category_id}", offset, len(items) > PAGE, PAGE)
    rows.append([button("‹ Категории", "catalog:0"), button("🏠 Меню", "home")])
    await show(update, f"📁 {category['name']}\n\n" + ("Выберите товар:" if items else "В этой категории пока нет товаров."), rows)


async def product_page(update, context, product_id, quantity=1, with_photo=False):
    item = available(db(context), product_id)
    if with_photo and item.get('photo_id'):
        try:
            await context.bot.send_photo(update.effective_chat.id, item['photo_id'])
        except TelegramError:
            log.warning("Product photo unavailable: product=%s", product_id)
    stock = "под заказ" if item['stock'] is None else f"{item['stock']} шт."
    text = (f"🎮 {item['title']}\n\n{item['description']}\n\n"
            f"Цена: {item['price']} ⭐ / шт.\nДоступно: {stock}\n"
            "Выдача вручную через бот после оплаты.")
    rows = []
    if item['stock'] != 0:
        validate_quantity(item, quantity, context)
        text += f"\n\nКоличество: {quantity}\nИтого: {item['price'] * quantity} ⭐"
        limit = min(context.bot_data['config'].max_quantity, item['stock'] or 100,
                    1_000_000 // item['price'])
        rows += [[button("−", f"q:{product_id}:{max(1, quantity-1)}"),
                  button(f"{quantity} шт. ✏️", f"qty:{product_id}"),
                  button("+", f"q:{product_id}:{min(limit, quantity+1)}")],
                 [button(f"Купить за {item['price'] * quantity} ⭐", f"buy:{product_id}:{quantity}")]]
    rows += [[button("💬 Уточнить у владельца", "s:open")],
             [button("‹ К товарам", f"cat:{item['category_id']}:0")]]
    # Sending after a photo keeps the action buttons below the image.
    if with_photo and item.get('photo_id'):
        await send_text(context.bot, update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(rows))
    else:
        await show(update, text, rows)


async def checkout(update, context, product_id, quantity):
    item = available(db(context), product_id)
    validate_quantity(item, quantity, context)
    nonce = secrets.token_hex(8)
    terms_text = db(context).get_setting('terms')
    context.user_data['checkout'] = {'nonce': nonce, 'product_id': product_id,
                                     'quantity': quantity, 'price': item['price'], 'terms': terms_text}
    await show(update, f"🧾 Проверьте заказ\n\n{item['title']}\n"
               f"{quantity} шт. × {item['price']} ⭐ = {quantity * item['price']} ⭐\n\n"
               f"📄 Условия покупки\n{terms_text}\n\n"
               "Нажимая кнопку ниже, вы принимаете эти условия и переходите к счёту Telegram.",
               [[button("✅ Принимаю условия · к оплате", f"pay:{nonce}")],
                [button("❌ Отменить оплату", f"cancelquote:{nonce}")],
                [button("‹ К товару", f"p:{product_id}")]])


async def send_invoice(update, context, nonce):
    quote = context.user_data.get('checkout')
    if not quote or quote['nonce'] != nonce:
        raise StoreError("Этот экран оплаты уже использован или устарел. Выберите товар заново.")
    item = available(db(context), quote['product_id'])
    validate_quantity(item, quote['quantity'], context)
    if item['price'] != quote['price'] or db(context).get_setting('terms') != quote['terms']:
        raise StoreError("Цена или условия изменились. Откройте товар заново и проверьте заказ.")
    context.user_data.pop('checkout', None)
    order = db(context).create_order(update.effective_user.id, item['id'], quote['quantity'], terms_text=quote['terms'])
    try:
        invoice = await context.bot.send_invoice(
            chat_id=update.effective_chat.id, title=item['title'][:32],
            description=f"Заказ #{order['id']} · {item['title']} · {order['quantity']} шт. Выдача владельцем через бот.",
            payload=order['invoice_payload'], provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"{order['quantity']} шт.", amount=order['total'])],
            start_parameter=f"order_{order['id']}",
            protect_content=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Оплатить {order['total']} ⭐", pay=True)],
                [button('❌ Отменить оплату', f"cancelpay:{order['id']}")],
            ]),
        )
    except BadRequest:
        db(context).cancel_pending_order(order['id'])
        raise StoreError("Не удалось создать счёт. Попробуйте позже или напишите в поддержку.") from None
    db(context).save_invoice_message(order['id'], update.effective_chat.id, invoice.message_id)
    notice = await show(update, f"🧾 Счёт для заказа #{order['id']} отправлен отдельным сообщением.\n\n"
               "Оплатите его в течение 15 минут. Заказ станет оплаченным после подтверждения Telegram.",
               [[button("📦 Проверить заказ", f"o:{order['id']}")],
                [button('❌ Отменить оплату', f"cancelpay:{order['id']}")], [button("🏠 Меню", "home")]])
    if notice:
        db(context).save_invoice_notice(order['id'], notice.message_id)


async def cancel_invoice(update, context, order_id):
    store = db(context)
    order = store.cancel_customer_order(order_id, update.effective_user.id)
    context.user_data.pop('checkout', None)
    invoice = store.get_invoice_message(order_id)
    text = f"❌ Оплата заказа #{order_id} отменена.\n\nСчёт больше недействителен. Вы можете выбрать другой товар."
    rows = [[button('🛍 Вернуться в каталог', 'catalog:0')], [button('📦 Мои заказы', 'orders:0')]]
    if invoice:
        try:
            await context.bot.delete_message(chat_id=invoice['chat_id'], message_id=invoice['message_id'])
        except TelegramError:
            # Even if Telegram cannot remove an old message, pre-checkout rejects
            # the cancelled invoice. Removing the Pay button is best-effort.
            try:
                await context.bot.edit_message_reply_markup(chat_id=invoice['chat_id'],
                                                            message_id=invoice['message_id'], reply_markup=None)
            except TelegramError:
                pass
        notice_id = invoice.get('notice_message_id')
        current_id = update.effective_message.message_id
        if notice_id and notice_id != current_id:
            try:
                await context.bot.edit_message_text(chat_id=invoice['chat_id'], message_id=notice_id,
                                                    text=text, reply_markup=InlineKeyboardMarkup(rows))
            except TelegramError:
                pass
    await show(update, text, rows)


async def order_page(update, context, order_id):
    order = db(context).get_order(order_id)
    if not order or order['user_id'] != update.effective_user.id:
        raise StoreError("Заказ не найден.")
    rows = [[button('❌ Отменить оплату', f"cancelpay:{order_id}")]] if order['status'] == 'pending' else []
    rows += [[button("💬 Вопрос по заказу", "s:open")], [button("‹ Мои заказы", "orders:0")]]
    await show(update, order_text(order), rows)


async def orders_page(update, context, offset=0):
    orders = db(context).list_orders(user_id=update.effective_user.id, limit=PAGE+1, offset=offset)
    rows = [[button(f"#{o['id']} · {o['product_title'][:30]} · {o['total']} ⭐", f"o:{o['id']}")] for o in orders[:PAGE]]
    rows += pagination("orders", offset, len(orders) > PAGE, PAGE)
    rows.append([button("🏠 Меню", "home")])
    await show(update, "📦 Мои заказы\n\n" + ("Выберите заказ, чтобы посмотреть статус." if orders else "Заказов пока нет."), rows)


async def customer_callback(update, context):
    await update.callback_query.answer()
    await remember(update, context)
    parts = update.callback_query.data.split(':')
    context.user_data.pop('flow', None)
    action = parts[0]
    if action == 'home':
        await start(update, context)
    elif action == 'terms':
        await terms(update, context)
    elif action == 'catalog':
        await catalogue(update, context, max(0, int(parts[1])))
    elif action == 'cat':
        await category_page(update, context, int(parts[1]), max(0, int(parts[2])))
    elif action == 'p':
        await product_page(update, context, int(parts[1]), with_photo=True)
    elif action == 'q':
        await product_page(update, context, int(parts[1]), int(parts[2]))
    elif action == 'qty':
        available(db(context), int(parts[1]))
        context.user_data['flow'] = {'kind': 'quantity', 'product_id': int(parts[1])}
        await show(update, f"✏️ Введите количество от 1 до {context.bot_data['config'].max_quantity}.\nОтмена: /cancel",
                   [[button("‹ К товару", f"p:{parts[1]}")]])
    elif action == 'buy':
        await checkout(update, context, int(parts[1]), int(parts[2]))
    elif action == 'pay':
        await send_invoice(update, context, parts[1])
    elif action == 'cancelquote':
        quote = context.user_data.get('checkout')
        if not quote or quote['nonce'] != parts[1]:
            raise StoreError('Этот экран оплаты устарел. Текущий счёт можно отменить в разделе «Мои заказы».')
        context.user_data.pop('checkout', None)
        await show(update, '❌ Оплата отменена. Счёт не создан.', [[button('🛍 Каталог', 'catalog:0')]])
    elif action == 'cancelpay':
        await cancel_invoice(update, context, int(parts[1]))
    elif action == 'orders':
        await orders_page(update, context, max(0, int(parts[1])))
    elif action == 'o':
        await order_page(update, context, int(parts[1]))
    else:
        raise StoreError("Эта кнопка устарела. Откройте /start.")


async def quantity_input(update, context):
    flow = context.user_data.get('flow', {})
    if flow.get('kind') != 'quantity':
        return False
    text = update.effective_message.text or ''
    if not text.isascii() or not text.isdigit() or len(text) > 3:
        raise StoreError("Введите количество целым числом, например 2.")
    await product_page(update, context, flow['product_id'], int(text))
    context.user_data.pop('flow', None)
    return True


async def precheckout(update, context):
    query = update.pre_checkout_query
    try:
        db(context).approve_checkout(query.invoice_payload, query.from_user.id,
                                     query.currency, query.total_amount, query.id)
    except StoreError as exc:
        await query.answer(ok=False, error_message=str(exc)[:200])
        return
    await query.answer(ok=True)


async def successful_payment(update, context):
    payment = update.effective_message.successful_payment
    try:
        db(context).record_payment(payment.invoice_payload, update.effective_user.id,
                                   payment.currency, payment.total_amount, payment.telegram_payment_charge_id)
    except StoreError as exc:
        db(context).log_payment_issue(payment.invoice_payload, update.effective_user.id,
                                      payment.currency, payment.total_amount,
                                      payment.telegram_payment_charge_id, str(exc))
        await update.effective_message.reply_text("Платёж получен Telegram, но заказ требует проверки владельцем. Напишите /paysupport.")
        await context.bot.send_message(context.bot_data['config'].owner_id,
                                       f"⚠️ Требуется сверка платежа клиента {update.effective_user.id}.\n"
                                       f"Сумма: {payment.total_amount} {payment.currency}\n"
                                       f"Причина: {exc}\nДанные сохранены в базе: python -m shop --payment-issues")
        return
    # Durable flags are drained independently so notification failures cannot undo a payment.
    context.bot_data['notification_event'].set()


async def refunded_payment(update, context):
    payment = update.effective_message.refunded_payment
    order = db(context).get_order_by_payload(payment.invoice_payload)
    if (not order or order['user_id'] != update.effective_user.id or payment.currency != 'XTR'
            or payment.total_amount != order['total'] or payment.telegram_payment_charge_id != order['charge_id']):
        log.error("Refund event did not match an order: user=%s", update.effective_user.id)
        return
    db(context).mark_refunded(order['id'], charge_id=payment.telegram_payment_charge_id)


async def deliver_notifications(application):
    store, bot = application.bot_data['db'], application.bot
    config = application.bot_data['config']
    async with application.bot_data['notification_lock']:
        orders = store.pending_notifications(after_id=application.bot_data.get('notification_cursor', 0))
        if not orders:
            orders = store.pending_notifications()
        if orders:
            application.bot_data['notification_cursor'] = orders[-1]['id']
        for order in orders:
            for who in ('buyer', 'owner'):
                order = store.get_order(order['id'])
                if order['status'] not in {'paid', 'fulfilled'}:
                    break
                if order[f'{who}_notified']:
                    continue
                try:
                    if who == 'buyer':
                        await send_text(bot, order['user_id'], "✅ Оплата получена!\n\n" + order_text(order)
                                               + "\n\n" + store.get_setting('after_purchase_text'),
                                               reply_markup=InlineKeyboardMarkup([[button("💬 Поддержка", "s:open")],
                                                                                  [button("📦 Мои заказы", "orders:0")]]))
                    else:
                        user = store.get_user(order['user_id'])
                        warning = "\n⚠️ Недостаток товара при восстановлении платежа. Свяжитесь с клиентом." if order.get('inventory_issue') else ''
                        sent = await send_text(bot, config.owner_id, "💰 Новая оплата\n\n" + order_text(order)
                                                      + '\n\n' + customer_contact(order['user_id'], user) + warning,
                                                      reply_markup=InlineKeyboardMarkup(customer_actions(order['user_id'], user) +
                                                                                        [[button("⚙️ Открыть заказ", f"a:order:{order['id']}")]]))
                        store.link_message(sent.message_id, order['user_id'])
                    store.mark_notified(order['id'], who)
                except TelegramError as exc:
                    # Retry after reconnect / restart. Never log request URLs (contain the token).
                    log.warning("Order notification failed: order=%s target=%s error=%s", order['id'], who, type(exc).__name__)


async def notification_worker(application):
    event = application.bot_data['notification_event']
    while True:
        event.clear()
        try:
            await deliver_notifications(application)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Notification worker error: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
