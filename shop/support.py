"""Private support relay. Only the configured owner can address a customer."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .common import button, customer_actions, db, owner, remember, require_owner, show
from .store import StoreError


def copyable(message):
    return bool(message.text or message.photo or message.document or message.video
                or message.voice or message.audio or message.sticker or message.animation
                or message.video_note)


async def open_support(update, context):
    await remember(update, context)
    context.user_data.clear()
    if owner(update, context):
        await show(update, "💬 Переписка с клиентами\n\n"
                   "Ответьте на уведомление о заказе или на сообщение клиента функцией «Ответить» в Telegram. "
                   "Также можно выбрать клиента в панели владельца.",
                   [[button("👥 Клиенты", "a:users:0")], [button("⚙️ Панель владельца", "a:home")]])
        return
    context.user_data['flow'] = {'kind': 'support'}
    rows = [[button("✅ Завершить переписку", "s:stop")]]
    manager = db(context).get_setting('manager_username').lstrip('@')
    if manager:
        rows.insert(0, [InlineKeyboardButton(f"Менеджер @{manager}", url=f"https://t.me/{manager}")])
    await show(update, "💬 Поддержка магазина\n\nНапишите ваш вопрос или отправьте фото, файл, видео либо голосовое. "
               "Сообщение получит владелец, его ответ придёт сюда. По заказу укажите его номер.\n\n"
               "Вопросы оплаты решает владелец магазина; поддержка Telegram не обслуживает эти покупки.\n"
               "Вы в режиме переписки до нажатия кнопки ниже или команды /cancel.", rows)


async def support_callback(update, context):
    parts = update.callback_query.data.split(':')
    if parts[1] == 'write':
        if not await require_owner(update, context):
            return
        await update.callback_query.answer()
        user_id = int(parts[2])
        user = db(context).get_user(user_id)
        if not user or user_id == context.bot_data['config'].owner_id:
            raise StoreError("Выберите клиента, который уже открывал бот.")
        context.user_data.clear()
        context.user_data['flow'] = {'kind': 'owner_reply', 'user_id': user_id}
        await show(update, f"✉️ Сообщение клиенту\n\n{user['full_name']} · ID {user_id}\n\n"
                   "Отправьте одно сообщение, ключ, фото или файл. Оно придёт клиенту от имени магазина. "
                   "Для следующего сообщения снова нажмите «Написать».\nОтмена: /cancel",
                   [[button("Отмена", "a:users:0")]])
    elif parts[1] == 'open':
        await update.callback_query.answer()
        await open_support(update, context)
    elif parts[1] == 'stop':
        await update.callback_query.answer()
        from .customer import start
        await start(update, context)
    else:
        raise StoreError("Эта кнопка устарела. Откройте /support.")


async def relay_message(update, context):
    message = update.effective_message
    flow = context.user_data.get('flow', {})
    store = db(context)
    if owner(update, context):
        # An explicit Telegram reply takes priority over a stale recipient draft.
        reply = message.reply_to_message
        target = store.linked_user(reply.message_id) if reply else None
        if target is None and flow.get('kind') == 'owner_reply':
            target = flow['user_id']
        if target is None:
            return False
        if not copyable(message):
            raise StoreError("Отправьте текст, фото, файл, видео, голосовое или стикер.")
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=message.chat_id,
                                           message_id=message.message_id,
                                           reply_markup=InlineKeyboardMarkup([[button("💬 Ответить магазину", "s:open")]]))
        except TelegramError:
            raise StoreError("Сообщение не доставлено. Клиент мог заблокировать бот, либо Telegram временно недоступен. Попробуйте ещё раз.") from None
        context.user_data.pop('flow', None)
        confirmation = await message.reply_text(f"✅ Доставлено клиенту {target}.",
                                                reply_markup=InlineKeyboardMarkup([[button("✉️ Написать ещё", f"s:write:{target}")]]))
        store.link_message(confirmation.message_id, target)
        return True
    if flow.get('kind') != 'support':
        return False
    if not copyable(message):
        raise StoreError("Этот тип сообщения не поддерживается. Отправьте текст, фото, файл или голосовое.")
    await remember(update, context)
    user = update.effective_user
    recipient = context.bot_data['config'].owner_id
    username = f" @{user.username}" if user.username else ""
    try:
        header = await context.bot.send_message(recipient, f"💬 Клиент: {user.full_name}{username}\nID: {user.id}\n"
                                                 "Ответьте на это сообщение или на копию ниже.",
                                                 reply_markup=InlineKeyboardMarkup(customer_actions(user.id, store.get_user(user.id))))
        store.link_message(header.message_id, user.id)
        copied = await context.bot.copy_message(chat_id=recipient, from_chat_id=message.chat_id,
                                                message_id=message.message_id)
        store.link_message(copied.message_id, user.id)
    except TelegramError:
        raise StoreError("Не удалось доставить сообщение владельцу. Попробуйте позже; текст можно отправить повторно.") from None
    await message.reply_text("✅ Сообщение передано владельцу. Ответ придёт в этот чат.",
                             reply_markup=InlineKeyboardMarkup([[button("🏠 Завершить и открыть меню", "home")]]))
    return True
