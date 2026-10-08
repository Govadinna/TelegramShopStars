"""Application wiring, startup and user-serialized update processing."""
import argparse
import asyncio
from contextlib import suppress
import json
import logging
from pathlib import Path
import sys
from weakref import WeakValueDictionary

from telegram import BotCommand, BotCommandScopeChat
from telegram.error import TelegramError
from telegram.ext import (Application, BaseUpdateProcessor, CallbackQueryHandler,
                          CommandHandler, MessageHandler, PreCheckoutQueryHandler, filters)

from . import admin, customer, support
from .common import button, db, owner, remember, show
from .config import Config, ROOT
from .store import Store, StoreError

log = logging.getLogger(__name__)


class UserUpdateProcessor(BaseUpdateProcessor):
    """One conversation at a time per user; checkout never waits for media relays."""
    def __init__(self):
        # BaseUpdateProcessor's outer semaphore must not queue checkout behind
        # slow message copies. Normal work has its own bounded semaphore below.
        super().__init__(sys.maxsize)
        self.locks = WeakValueDictionary()
        self.normal_slots = asyncio.Semaphore(32)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        if update.pre_checkout_query:
            await coroutine
            return
        user_id = update.effective_user.id if update.effective_user else 0
        lock = self.locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[user_id] = lock
        async with lock:
            async with self.normal_slots:
                await coroutine


async def private_guard(update, context):
    if update.effective_chat and update.effective_chat.type == 'private':
        return True
    if update.callback_query:
        await update.callback_query.answer("Откройте личный чат с ботом.", show_alert=True)
    return False


async def identify(update, context):
    if await private_guard(update, context):
        await update.effective_message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def cancel(update, context):
    context.user_data.clear()
    await customer.start(update, context)


async def privacy(update, context):
    await show(update, "🔐 Данные магазина\n\nБот сохраняет ваш Telegram ID, имя, username, заказы и сведения об оплате. "
               "Переписка копируется в личный чат владельца. Данные нужны для выдачи покупок, ответов и возвратов. "
               "По вопросам ваших данных напишите владельцу: /support.",
               [[button("🏠 Меню", "home")]])


async def command(update, context):
    if not await private_guard(update, context):
        return
    await remember(update, context)
    name = update.effective_message.text.split()[0].split('@')[0].lower()
    commands = {'/start': customer.start, '/catalog': customer.catalogue,
                '/orders': customer.orders_page, '/terms': customer.terms,
                '/support': support.open_support, '/paysupport': support.open_support,
                '/admin': admin.admin_command, '/cancel': cancel, '/id': identify,
                '/privacy': privacy}
    handler = commands.get(name)
    if handler:
        context.user_data.clear()
        await handler(update, context)
    else:
        await update.effective_message.reply_text("Откройте /start или напишите /support.")


async def callback(update, context):
    if not await private_guard(update, context):
        return
    data = update.callback_query.data
    if not isinstance(data, str):
        await update.callback_query.answer("Кнопка устарела. Откройте /start.")
        return
    try:
        if data.startswith('a:'):
            await admin.admin_callback(update, context)
        elif data.startswith('s:'):
            await support.support_callback(update, context)
        else:
            await customer.customer_callback(update, context)
    except (StoreError, ValueError, IndexError) as exc:
        text = str(exc) if isinstance(exc, StoreError) else "Кнопка устарела. Откройте /start."
        await show(update, "ℹ️ " + text, [[button("🏠 Главное меню", "home")]])


async def incoming(update, context):
    if not await private_guard(update, context):
        return
    await remember(update, context)
    try:
        if owner(update, context) and await admin.admin_input(update, context):
            return
        if await customer.quantity_input(update, context):
            return
        if await support.relay_message(update, context):
            return
        await update.effective_message.reply_text("Выберите товар в /catalog или откройте /support, чтобы написать владельцу."
                                                 if not owner(update, context) else
                                                 "Откройте /admin или ответьте на сообщение клиента функцией «Ответить».")
    except StoreError as exc:
        await update.effective_message.reply_text("ℹ️ " + str(exc))


async def handle_error(update, context):
    # Do not log the entire update, message body, token or HTTP URL.
    log.error("Update failed: id=%s error=%s", getattr(update, 'update_id', None), type(context.error).__name__)
    if update and update.effective_message:
        with suppress(TelegramError):
            await update.effective_message.reply_text("Не удалось выполнить действие. Попробуйте ещё раз или откройте /support.")


async def post_init(application):
    commands = [BotCommand('start', 'Главное меню'), BotCommand('catalog', 'Каталог'),
                BotCommand('orders', 'Мои заказы'), BotCommand('support', 'Поддержка'),
                BotCommand('paysupport', 'Вопросы оплаты и возвратов'), BotCommand('terms', 'Условия покупки'),
                BotCommand('cancel', 'Отменить ввод'), BotCommand('id', 'Мой Telegram ID')]
    try:
        await application.bot.set_my_commands(commands)
        await application.bot.set_my_commands(commands + [BotCommand('admin', 'Панель владельца')],
                                               scope=BotCommandScopeChat(application.bot_data['config'].owner_id))
    except TelegramError as exc:
        log.warning("Command menu setup: %s. Owner must press /start.", type(exc).__name__)
    application.bot_data['notification_task'] = asyncio.create_task(customer.notification_worker(application))
    log.info("Бот запущен. Остановка: Ctrl+C.")


async def post_stop(application):
    task = application.bot_data.get('notification_task')
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def post_shutdown(application):
    application.bot_data['db'].close()


def builder(config):
    result = Application.builder().token(config.bot_token).connection_pool_size(64).connect_timeout(10).read_timeout(15)
    if config.test_environment:
        result = result.base_url(lambda token: f"https://api.telegram.org/bot{token}/test")
        result = result.base_file_url(lambda token: f"https://api.telegram.org/file/bot{token}/test")
    return result


def build_application(config, store):
    app = (builder(config).concurrent_updates(UserUpdateProcessor()).post_init(post_init)
           .post_stop(post_stop).post_shutdown(post_shutdown).build())
    app.bot_data.update(config=config, db=store, notification_event=asyncio.Event(), notification_lock=asyncio.Lock())
    app.add_handler(PreCheckoutQueryHandler(customer.precheckout))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.SUCCESSFUL_PAYMENT, customer.successful_payment))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.StatusUpdate.REFUNDED_PAYMENT, customer.refunded_payment))
    app.add_handler(MessageHandler(filters.COMMAND, command))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.StatusUpdate.ALL, incoming))
    app.add_error_handler(handle_error)
    return app


class ProcessLock:
    """Prevent two polling processes with the same token on this computer."""
    def __init__(self, config):
        folder = ROOT / 'data'
        folder.mkdir(parents=True, exist_ok=True)
        self.file = open(folder / f".polling-{config.bot_token.split(':')[0]}-{int(config.test_environment)}.lock", 'a+b')

    def __enter__(self):
        try:
            if sys.platform == 'win32':
                import msvcrt
                self.file.seek(0)
                self.file.write(b'0')
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("Этот бот уже запущен на компьютере. Остановите второй экземпляр.") from None
        return self

    def __exit__(self, *_):
        self.file.close()


def seed_demo(store):
    if store.get_setting('demo_seeded'):
        print("Демонстрационные товары уже добавлены.")
        return
    category_id = store.add_category('🧪 Тестовые товары')
    store.add_product(category_id, 'ТЕСТ · Проверка оплаты',
                      'Тестовый товар без игрового ключа. В обычной среде оплата спишет 1 настоящую Star. '
                      'Владелец может вернуть её из заказа в админ-панели.', 1, stock=10)
    store.set_setting('demo_seeded', '1')
    print("Добавлен тестовый товар за 1 ⭐. Удалите категорию после проверки.")


def main(argv=None):
    parser = argparse.ArgumentParser(description='Магазин Telegram с оплатой Stars')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--setup-id', action='store_true', help='Запустить только /id; OWNER_ID пока не нужен')
    mode.add_argument('--check', action='store_true', help='Проверить настройки и базу без связи с Telegram')
    mode.add_argument('--seed-demo', action='store_true', help='Добавить один тестовый товар')
    mode.add_argument('--payment-issues', action='store_true', help='Показать платежи для ручной сверки')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try:
        config = Config.load(setup=args.setup_id)
        if args.setup_id:
            print("Режим настройки: напишите своему боту /id. Скопируйте ID своего аккаунта в .env, затем Ctrl+C.")
            with ProcessLock(config):
                app = builder(config).build()
                app.add_handler(CommandHandler(['start', 'id'], identify))
                app.run_polling(allowed_updates=['message'], drop_pending_updates=False)
            return 0
        store = Store(config.db_path)
        # A database belongs to one Telegram bot; changing OWNER_ID is permitted.
        identity = config.bot_token.split(':')[0] + ('-test' if config.test_environment else '-live')
        saved = store.get_setting('_bot_identity')
        if saved and saved != identity:
            store.close()
            raise ValueError("Эта база принадлежит другому боту. Задайте другой DATABASE_PATH.")
        store.set_setting('_bot_identity', identity)
        if args.check or args.seed_demo or args.payment_issues:
            try:
                if args.seed_demo:
                    seed_demo(store)
                elif args.payment_issues:
                    print(json.dumps(store.list_payment_issues(), ensure_ascii=False, indent=2))
                else:
                    print(f"Настройки и база доступны. Среда: {'тестовая' if config.test_environment else 'обычная (реальные Stars)'}.\n"
                          "Токен не проверялся через Telegram. Для запуска: python -m shop")
            finally:
                store.close()
            return 0
        with ProcessLock(config):
            app = build_application(config, store)
            app.run_polling(allowed_updates=['message', 'callback_query', 'pre_checkout_query'],
                            drop_pending_updates=False, bootstrap_retries=3)
        return 0
    except (ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except TelegramError as exc:
        print(f"Ошибка Telegram ({type(exc).__name__}). Проверьте токен, доступ к сети и что бот запущен только один раз.", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
