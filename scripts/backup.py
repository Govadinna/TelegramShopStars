"""Create a consistent SQLite backup without overwriting an existing file."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    parser = argparse.ArgumentParser(description="Безопасная резервная копия базы магазина.")
    parser.add_argument("--database", default="data/shop.sqlite3", help="База SQLite; относительный путь от папки проекта.")
    parser.add_argument("--output", default=f"backups/shop-{stamp}.sqlite3", help="Новый файл копии; существующие файлы не перезаписываются.")
    args = parser.parse_args()
    source = project_path(args.database)
    destination = project_path(args.output)
    created = False
    try:
        if not source.is_file():
            raise ValueError(f"База не найдена: {source}")
        if source == destination:
            raise ValueError("Файл копии должен отличаться от исходной базы.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        created = True
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as origin:
            with closing(sqlite3.connect(destination, timeout=30)) as target:
                origin.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Проверка копии не прошла.")
        print(f"Резервная копия сохранена: {destination}")
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        if created:
            destination.unlink(missing_ok=True)
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
