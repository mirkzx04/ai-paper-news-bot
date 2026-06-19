"""Dev helper — clear the `seen` table so the next digest re-sends papers.

Useful for dry-runs: papers are de-duplicated by `seen`, so to receive them
again (e.g. after a /clear wiped the chat) you must forget them first.

    python tools/reset_seen.py            # data/bot.db
    python tools/reset_seen.py path.db    # custom db
"""

import sqlite3
import sys


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/bot.db"
    con = sqlite3.connect(db)
    try:
        n = con.execute("DELETE FROM seen").rowcount
        con.commit()
    finally:
        con.close()
    print(f"cleared {n} seen ids from {db}")


if __name__ == "__main__":
    main()
