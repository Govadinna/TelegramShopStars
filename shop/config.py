"""Environment configuration. Secrets are never written to logs."""
from dataclasses import dataclass
from pathlib import Path
import os
import re

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    db_path: Path
    test_environment: bool = False
    max_quantity: int = 100

    @classmethod
    def load(cls, setup: bool = False):
        load_dotenv(ROOT / ".env", override=False)
        token = os.getenv("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажите BOT_TOKEN от @BotFather в файле .env.")
        try:
            owner_id = int(os.getenv("OWNER_ID", "0").strip() or "0")
        except ValueError:
            raise ValueError("OWNER_ID должен быть числовым Telegram ID.") from None
        if not setup and owner_id <= 0:
            raise ValueError("Заполните OWNER_ID. Узнать свой ID: python -m shop --setup-id")
        flag = os.getenv("TELEGRAM_TEST_ENV", "false").lower().strip()
        if flag not in {"true", "false"}:
            raise ValueError("TELEGRAM_TEST_ENV: только true или false.")
        try:
            max_quantity = int(os.getenv("MAX_QUANTITY", "100"))
        except ValueError:
            raise ValueError("MAX_QUANTITY: целое число от 1 до 100.") from None
        if not 1 <= max_quantity <= 100:
            raise ValueError("MAX_QUANTITY: целое число от 1 до 100.")
        path = Path(os.getenv("DATABASE_PATH", "data/shop.sqlite3"))
        if not path.is_absolute():
            path = ROOT / path
        # A test bot must never mutate production orders or customer records.
        if flag == "true":
            path = path.with_name(path.stem + "-test" + path.suffix)
        return cls(token, owner_id, path, flag == "true", max_quantity)
