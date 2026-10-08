"""Persistent shop data and atomic Telegram Stars payment bookkeeping."""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class StoreError(ValueError):
    """A safe, user-facing validation error."""


DEFAULT_SETTINGS = {
    "shop_name": "🎮 Game Store",
    "welcome_text": "Игры, ключи и аккаунты — выбирайте в каталоге.",
    "after_purchase_text": "Спасибо за покупку! Владелец отправит товар через этот бот. Если есть вопрос, нажмите «Поддержка».",
    "manager_username": "",
    "terms": "Товар выдаётся владельцем вручную через этот бот после оплаты. Перед оплатой уточните наличие, регион и срок выдачи через поддержку. По вопросам заказа и возврата: /paysupport.",
}
STATUSES = {"pending", "awaiting_payment", "paid", "fulfilled", "refunded", "cancelled"}
MAX_STARS = 1_000_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="microseconds")


def _integer(value: Any, label: str, minimum: int = 1, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StoreError(f"{label}: требуется целое число от {minimum} до {maximum}.")
    return value


def _text(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise StoreError(f"{label}: требуется текст.")
    value = value.strip()
    if (not value and not empty) or len(value) > maximum:
        raise StoreError(f"{label}: допустимо {'0' if empty else '1'}–{maximum} символов.")
    return value


def _active(value: Any) -> int:
    if not isinstance(value, (bool, int)) or value not in (0, 1):
        raise StoreError("Видимость должна быть включена или выключена.")
    return int(value)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                full_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL REFERENCES categories(id),
                title TEXT NOT NULL, description TEXT NOT NULL,
                price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 1000000),
                stock INTEGER CHECK(stock IS NULL OR stock >= 0), photo_id TEXT,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                product_title TEXT NOT NULL, unit_price INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100),
                total INTEGER NOT NULL CHECK(total BETWEEN 1 AND 1000000),
                terms_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'awaiting_payment', 'paid', 'fulfilled', 'refunded', 'cancelled')),
                invoice_payload TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                charge_id TEXT UNIQUE, precheckout_id TEXT UNIQUE, approved_at TEXT,
                paid_at TEXT, fulfilled_at TEXT, refunded_at TEXT,
                inventory_issue INTEGER NOT NULL DEFAULT 0 CHECK(inventory_issue IN (0, 1)),
                buyer_notified INTEGER NOT NULL DEFAULT 0 CHECK(buyer_notified IN (0, 1)),
                owner_notified INTEGER NOT NULL DEFAULT 0 CHECK(owner_notified IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS orders_user_created ON orders(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS orders_status ON orders(status, id DESC);
            CREATE INDEX IF NOT EXISTS products_category ON products(category_id, id);
            CREATE TABLE IF NOT EXISTS invoice_messages (
                order_id INTEGER PRIMARY KEY REFERENCES orders(id),
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                notice_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS payment_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
                user_id INTEGER NOT NULL, currency TEXT NOT NULL,
                total INTEGER NOT NULL, charge_id TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_links (
                owner_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id)
            );
        """)
        with self._transaction():
            self._conn.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", DEFAULT_SETTINGS.items()
            )

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._conn.execute(sql, args).fetchall()]

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        return _integer(limit, "Размер страницы", 1, 100), _integer(offset, "Смещение", 0)

    def upsert_user(self, user_id: int, username: str = "", full_name: str = "") -> None:
        user_id = _integer(user_id, "ID пользователя")
        username = _text(username or "", "Имя пользователя", 64, empty=True)
        full_name = _text(full_name or "", "Имя", 256, empty=True)
        now = _stamp()
        self._conn.execute(
            "INSERT INTO users(id, username, full_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name, "
            "updated_at=excluded.updated_at", (user_id, username, full_name, now, now)
        )

    def get_user(self, user_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE id=?", (user_id,))

    def list_users(self, limit: int = 10, offset: int = 0) -> list[dict]:
        limit, offset = self._page(limit, offset)
        return self._all("SELECT * FROM users ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset))

    def list_categories(self, include_hidden: bool = False) -> list[dict]:
        return self._all("SELECT * FROM categories WHERE deleted=0" + ("" if include_hidden else " AND active=1") + " ORDER BY id")

    def get_category(self, category_id: int) -> dict | None:
        return self._one("SELECT * FROM categories WHERE id=?", (category_id,))

    def add_category(self, name: str) -> int:
        cursor = self._conn.execute("INSERT INTO categories(name) VALUES (?)", (_text(name, "Название категории", 100),))
        return int(cursor.lastrowid)

    def update_category(self, category_id: int, name: str | None = None, active: bool | None = None) -> None:
        fields = {}
        if name is not None:
            fields["name"] = _text(name, "Название категории", 100)
        if active is not None:
            fields["active"] = _active(active)
        with self._transaction():
            category = self.get_category(category_id)
            if not category or category["deleted"]:
                raise StoreError("Категория удалена или не найдена.")
            if fields:
                self._conn.execute(
                    "UPDATE categories SET " + ", ".join(f"{key}=?" for key in fields) + " WHERE id=?",
                    (*fields.values(), category_id),
                )

    def delete_category(self, category_id: int) -> None:
        with self._transaction():
            if not self.get_category(category_id):
                raise StoreError("Категория не найдена.")
            self._conn.execute("UPDATE categories SET active=0, deleted=1 WHERE id=?", (category_id,))
            self._conn.execute("UPDATE products SET active=0, deleted=1 WHERE category_id=?", (category_id,))

    def list_products(self, category_id: int | None = None, include_hidden: bool = False,
                      limit: int = 10, offset: int = 0) -> list[dict]:
        limit, offset = self._page(limit, offset)
        conditions = ["p.deleted=0", "c.deleted=0"]
        args: list[Any] = []
        if not include_hidden:
            conditions.extend(["p.active=1", "c.active=1"])
        if category_id is not None:
            conditions.append("p.category_id=?")
            args.append(category_id)
        args.extend([limit, offset])
        return self._all(
            "SELECT p.* FROM products p JOIN categories c ON c.id=p.category_id WHERE "
            + " AND ".join(conditions) + " ORDER BY p.id LIMIT ? OFFSET ?", tuple(args)
        )

    def get_product(self, product_id: int) -> dict | None:
        return self._one("SELECT * FROM products WHERE id=?", (product_id,))

    def _product_fields(self, fields: dict) -> dict:
        allowed = {"category_id", "title", "description", "price", "stock", "photo_id", "active"}
        if set(fields) - allowed:
            raise StoreError("Недопустимое поле товара.")
        result = dict(fields)
        for key, value in fields.items():
            if key == "category_id":
                result[key] = _integer(value, "ID категории")
            elif key == "title":
                result[key] = _text(value, "Название товара", 100)
            elif key == "description":
                result[key] = _text(value, "Описание товара", 2500, empty=True)
            elif key == "price":
                result[key] = _integer(value, "Цена в Stars", 1, MAX_STARS)
            elif key == "stock":
                result[key] = None if value is None else _integer(value, "Остаток", 0)
            elif key == "photo_id":
                result[key] = None if value is None else _text(value, "Фото", 2048)
            elif key == "active":
                result[key] = _active(value)
        return result

    def _check_category(self, category_id: int) -> None:
        category = self.get_category(category_id)
        if not category or category["deleted"]:
            raise StoreError("Категория удалена или не найдена.")

    def add_product(self, category_id: int, title: str, description: str, price: int,
                    stock: int | None = None, photo_id: str | None = None) -> int:
        fields = self._product_fields(dict(category_id=category_id, title=title, description=description,
                                           price=price, stock=stock, photo_id=photo_id))
        with self._transaction():
            self._check_category(fields["category_id"])
            cursor = self._conn.execute(
                "INSERT INTO products(category_id, title, description, price, stock, photo_id) VALUES (?, ?, ?, ?, ?, ?)",
                tuple(fields.values()),
            )
            return int(cursor.lastrowid)

    def update_product(self, product_id: int, **fields: Any) -> None:
        fields = self._product_fields(fields)
        with self._transaction():
            product = self.get_product(product_id)
            if not product or product["deleted"]:
                raise StoreError("Товар удалён или не найден.")
            if "category_id" in fields:
                self._check_category(fields["category_id"])
            if fields:
                self._conn.execute(
                    "UPDATE products SET " + ", ".join(f"{key}=?" for key in fields) + " WHERE id=?",
                    (*fields.values(), product_id),
                )

    def delete_product(self, product_id: int) -> None:
        if not self.get_product(product_id):
            raise StoreError("Товар не найден.")
        self._conn.execute("UPDATE products SET active=0, deleted=1 WHERE id=?", (product_id,))

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        key = _text(key, "Настройка", 100)
        value = _text(value, "Значение настройки", 5000, empty=True)
        self._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _sale_product(self, product_id: int) -> dict:
        product = self.get_product(product_id)
        if not product or not product["active"] or product["deleted"]:
            raise StoreError("Товар сейчас недоступен. Откройте каталог заново.")
        category = self.get_category(product["category_id"])
        if not category or not category["active"] or category["deleted"]:
            raise StoreError("Категория сейчас недоступна. Откройте каталог заново.")
        return product

    def create_order(self, user_id: int, product_id: int, quantity: int,
                     terms_text: str | None = None) -> dict:
        _integer(user_id, "ID пользователя")
        _integer(product_id, "ID товара")
        quantity = _integer(quantity, "Количество", 1, 100)
        if terms_text is not None:
            terms_text = _text(terms_text, "Условия покупки", 5000, empty=True)
        with self._transaction():
            if not self.get_user(user_id):
                raise StoreError("Сначала отправьте /start.")
            product = self._sale_product(product_id)
            total = product["price"] * quantity
            if total > MAX_STARS:
                raise StoreError("Сумма заказа не может превышать 1 000 000 Stars.")
            if product["stock"] is not None and product["stock"] < quantity:
                raise StoreError("Недостаточно товара в наличии.")
            now = _now()
            # Expired draft invoices no longer count toward the anti-spam limit.
            self._conn.execute("UPDATE orders SET status='cancelled' WHERE user_id=? AND status='pending' AND expires_at<=?",
                               (user_id, _stamp(now)))
            count = self._one("SELECT COUNT(*) AS n FROM orders WHERE user_id=? AND status='pending'", (user_id,))["n"]
            if count >= 10:
                raise StoreError("У вас уже 10 неоплаченных счетов. Оплатите один или повторите позже.")
            cursor = self._conn.execute(
                "INSERT INTO orders(user_id, product_id, product_title, unit_price, quantity, total, terms_text, "
                "invoice_payload, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, product_id, product["title"], product["price"], quantity, total,
                 self.get_setting("terms") if terms_text is None else terms_text,
                 "order_" + secrets.token_urlsafe(24), _stamp(now), _stamp(now + timedelta(minutes=15))),
            )
            return self.get_order(int(cursor.lastrowid))

    def get_order(self, order_id: int) -> dict | None:
        return self._one("SELECT * FROM orders WHERE id=?", (order_id,))

    def get_order_by_payload(self, payload: str) -> dict | None:
        return self._one("SELECT * FROM orders WHERE invoice_payload=?", (payload,))

    def list_orders(self, user_id: int | None = None, limit: int = 10, offset: int = 0,
                    status: str | None = None) -> list[dict]:
        limit, offset = self._page(limit, offset)
        conditions, args = [], []
        if user_id is not None:
            conditions.append("user_id=?")
            args.append(user_id)
        if status is not None:
            if status not in STATUSES:
                raise StoreError("Неизвестный статус заказа.")
            conditions.append("status=?")
            args.append(status)
        args.extend([limit, offset])
        return self._all("SELECT * FROM orders" + (" WHERE " + " AND ".join(conditions) if conditions else "")
                         + " ORDER BY id DESC LIMIT ? OFFSET ?", tuple(args))

    def _match_payment(self, payload: str, user_id: int, currency: str, total: int) -> dict:
        if not isinstance(payload, str) or not payload:
            raise StoreError("Счёт не найден.")
        order = self.get_order_by_payload(payload)
        if not order:
            raise StoreError("Счёт не найден.")
        if order["user_id"] != _integer(user_id, "ID покупателя"):
            raise StoreError("Этот счёт принадлежит другому покупателю.")
        if currency != "XTR":
            raise StoreError("Оплата принимается только в Telegram Stars.")
        if _integer(total, "Сумма", 1, MAX_STARS) != order["total"]:
            raise StoreError("Сумма оплаты не совпадает со счётом.")
        return order

    def approve_checkout(self, payload: str, user_id: int, currency: str, total: int, query_id: str) -> dict:
        query_id = _text(query_id, "ID проверки оплаты", 512)
        with self._transaction():
            order = self._match_payment(payload, user_id, currency, total)
            if order["precheckout_id"] == query_id:
                return order
            if order["status"] != "pending":
                raise StoreError("Этот счёт уже обработан. Не оплачивайте его повторно.")
            if order["expires_at"] <= _stamp():
                raise StoreError("Срок действия счёта истёк. Оформите заказ заново.")
            if self._one("SELECT id FROM orders WHERE precheckout_id=?", (query_id,)):
                raise StoreError("Проверка оплаты уже относится к другому счёту.")
            product = self._sale_product(order["product_id"])
            if product["stock"] is not None:
                if product["stock"] < order["quantity"]:
                    raise StoreError("Товар закончился. Деньги не списаны.")
                self._conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (order["quantity"], product["id"]))
            self._conn.execute("UPDATE orders SET status='awaiting_payment', precheckout_id=?, approved_at=? WHERE id=?",
                               (query_id, _stamp(), order["id"]))
            return self.get_order(order["id"])

    def record_payment(self, payload: str, user_id: int, currency: str, total: int,
                       charge_id: str) -> tuple[dict, bool]:
        charge_id = _text(charge_id, "ID платежа", 512)
        with self._transaction():
            order = self._match_payment(payload, user_id, currency, total)
            if order["charge_id"]:
                if order["charge_id"] == charge_id:
                    return order, False
                raise StoreError("Для этого заказа уже сохранён другой платёж. Нужна проверка владельца.")
            if self._one("SELECT id FROM orders WHERE charge_id=?", (charge_id,)):
                raise StoreError("Этот платёж уже относится к другому заказу.")
            if order["status"] not in {"pending", "awaiting_payment", "cancelled"}:
                raise StoreError("Платёж нельзя применить к этому заказу. Нужна проверка владельца.")
            inventory_issue = 0
            if order["status"] != "awaiting_payment":
                # A successful Telegram event is financial truth, even if a draft
                # expired or stock/catalog changed before this recovery arrived.
                product = self.get_product(order["product_id"])
                category = self.get_category(product["category_id"]) if product else None
                if not product or product["deleted"] or not product["active"] or not category or category["deleted"] or not category["active"]:
                    inventory_issue = 1
                if product and product["stock"] is not None:
                    inventory_issue |= int(product["stock"] < order["quantity"])
                    self._conn.execute("UPDATE products SET stock=MAX(0, stock-?) WHERE id=?",
                                       (order["quantity"], product["id"]))
            self._conn.execute("UPDATE orders SET status='paid', charge_id=?, paid_at=?, inventory_issue=? WHERE id=?",
                               (charge_id, _stamp(), inventory_issue, order["id"]))
            return self.get_order(order["id"]), True

    def cancel_pending_order(self, order_id: int) -> None:
        self._conn.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='pending'", (order_id,))

    def cancel_customer_order(self, order_id: int, user_id: int) -> dict:
        """Cancel only before pre-checkout approval; never release an in-flight payment."""
        with self._transaction():
            order = self.get_order(order_id)
            if not order or order['user_id'] != user_id:
                raise StoreError('Заказ не найден.')
            if order['status'] == 'cancelled':
                return order
            if order['status'] == 'awaiting_payment':
                raise StoreError('Telegram уже обрабатывает платёж. Дождитесь результата; если оплата зависла, напишите /paysupport. Не оплачивайте заказ повторно.')
            if order['status'] != 'pending':
                raise StoreError('Этот заказ уже оплачен или возвращён. Для возврата обратитесь в /paysupport.')
            self._conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
            return self.get_order(order_id)

    def save_invoice_message(self, order_id: int, chat_id: int, message_id: int) -> None:
        _integer(chat_id, 'ID чата')
        _integer(message_id, 'ID сообщения')
        self._conn.execute('INSERT INTO invoice_messages(order_id, chat_id, message_id) VALUES (?, ?, ?) '
                           'ON CONFLICT(order_id) DO UPDATE SET chat_id=excluded.chat_id, message_id=excluded.message_id',
                           (order_id, chat_id, message_id))

    def save_invoice_notice(self, order_id: int, message_id: int) -> None:
        _integer(message_id, 'ID сообщения')
        self._conn.execute('UPDATE invoice_messages SET notice_message_id=? WHERE order_id=?', (message_id, order_id))

    def get_invoice_message(self, order_id: int) -> dict | None:
        return self._one('SELECT * FROM invoice_messages WHERE order_id=?', (order_id,))

    def mark_fulfilled(self, order_id: int) -> dict:
        with self._transaction():
            order = self.get_order(order_id)
            if not order:
                raise StoreError("Заказ не найден.")
            if order["status"] == "fulfilled":
                return order
            if order["status"] != "paid":
                raise StoreError("Выданным можно отметить только оплаченный заказ.")
            self._conn.execute("UPDATE orders SET status='fulfilled', fulfilled_at=? WHERE id=?", (_stamp(), order_id))
            return self.get_order(order_id)

    def mark_refunded(self, order_id: int, charge_id: str | None = None) -> dict:
        with self._transaction():
            order = self.get_order(order_id)
            if not order:
                raise StoreError("Заказ не найден.")
            if charge_id is not None and charge_id != order["charge_id"]:
                raise StoreError("ID возвращённого платежа не совпадает с заказом.")
            if order["status"] == "refunded":
                return order
            if order["status"] not in {"paid", "fulfilled"}:
                raise StoreError("Возврат возможен только для оплаченного заказа.")
            self._conn.execute("UPDATE orders SET status='refunded', refunded_at=? WHERE id=?", (_stamp(), order_id))
            return self.get_order(order_id)

    def pending_notifications(self, limit: int = 50, after_id: int = 0) -> list[dict]:
        _integer(limit, "Размер страницы", 1, 100)
        _integer(after_id, "Номер заказа", 0)
        return self._all("SELECT * FROM orders WHERE status IN ('paid', 'fulfilled') "
                         "AND (buyer_notified=0 OR owner_notified=0) AND id>? ORDER BY id LIMIT ?", (after_id, limit))

    def mark_notified(self, order_id: int, who: str) -> None:
        if who not in {"buyer", "owner"}:
            raise StoreError("Неизвестный получатель уведомления.")
        self._conn.execute(f"UPDATE orders SET {who}_notified=1 WHERE id=?", (order_id,))

    def stats(self) -> dict:
        return self._one("SELECT (SELECT COUNT(*) FROM users) AS users, "
                         "(SELECT COUNT(*) FROM products WHERE deleted=0) AS products, "
                         "(SELECT COUNT(*) FROM orders WHERE status IN ('paid', 'fulfilled')) AS paid_orders, "
                         "(SELECT COALESCE(SUM(total), 0) FROM orders WHERE status IN ('paid', 'fulfilled')) AS revenue, "
                         "(SELECT COUNT(*) FROM orders WHERE status='awaiting_payment') AS pending_orders")

    def link_message(self, owner_message_id: int, user_id: int) -> None:
        _integer(owner_message_id, "ID сообщения")
        _integer(user_id, "ID клиента")
        if not self.get_user(user_id):
            raise StoreError("Клиент не найден.")
        self._conn.execute("INSERT INTO message_links(owner_message_id, user_id) VALUES (?, ?) "
                           "ON CONFLICT(owner_message_id) DO UPDATE SET user_id=excluded.user_id", (owner_message_id, user_id))

    def linked_user(self, owner_message_id: int) -> int | None:
        row = self._one("SELECT user_id FROM message_links WHERE owner_message_id=?", (owner_message_id,))
        return row["user_id"] if row else None

    def log_payment_issue(self, payload: str, user_id: int, currency: str, total: int,
                          charge_id: str, reason: str) -> None:
        """Preserve unmatched successful-payment evidence for manual investigation.

        This ledger intentionally does not require an existing user or order.
        Repeated delivery of a charge does not create duplicate issue records.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO payment_issues(payload, user_id, currency, total, charge_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload, user_id, currency, total, charge_id, str(reason)[:4000], _stamp()),
        )

    def list_payment_issues(self, limit: int = 50, offset: int = 0) -> list[dict]:
        limit, offset = self._page(limit, offset)
        return self._all("SELECT * FROM payment_issues ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))

    def backup(self, destination: str | Path) -> None:
        destination = Path(destination).expanduser()
        if self.path != ":memory:" and destination.resolve() == Path(self.path).resolve():
            raise StoreError("Резервную копию нужно сохранить в другой файл.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup_connection = sqlite3.connect(destination)
        try:
            self._conn.backup(backup_connection)
        finally:
            backup_connection.close()
