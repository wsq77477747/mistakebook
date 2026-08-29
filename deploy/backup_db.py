import datetime as dt
import os
import sqlite3


DB_PATH = os.environ.get("SQL_WRONGBOOK_DB", "/app/data/sql_review.db")
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "backups")
KEEP_DAYS = 14


def main():
    if not os.path.exists(DB_PATH):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    now = dt.datetime.now()
    target = os.path.join(BACKUP_DIR, f"sql_review_{now:%Y%m%d_%H%M%S}.db")
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        if name.startswith("sql_review_") and name.endswith(".db"):
            modified = dt.datetime.fromtimestamp(os.path.getmtime(path))
            if modified < cutoff:
                os.remove(path)
    print(target)


if __name__ == "__main__":
    main()
