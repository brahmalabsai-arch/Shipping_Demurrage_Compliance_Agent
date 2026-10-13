"""Create the application role, app database and test database on the local PostgreSQL.

Idempotent — safe to re-run (it also re-syncs the role's password with ``.env``). Role name,
database name and password come from ``config.Settings``; psql prompts for the *superuser*
password itself, so that password is never written to disk.

    .venv\\Scripts\\python.exe -m scripts.setup_local_db [--superuser postgres] [--psql PATH]
    .venv\\Scripts\\python.exe -m scripts.setup_local_db --reset   # drop + recreate the app DB

``--reset`` deletes the application database and everything in it (after you type its name),
then creates it empty; follow with ``alembic upgrade head``, ``python -m scripts.seed`` and
``python -m scripts.index_rulebook`` (README, "Start over").
"""

import argparse
import glob
import shutil
import subprocess
import sys
from pathlib import Path

from config.settings import get_settings

SQL_FILE = Path(__file__).with_name("setup_local_db.sql")
WINDOWS_PSQL_GLOB = r"C:\Program Files\PostgreSQL\*\bin\psql.exe"


def find_psql() -> str | None:
    """Prefer psql on PATH; otherwise the newest version under the default Windows install dir."""
    on_path = shutil.which("psql")
    if on_path:
        return on_path
    candidates = sorted(glob.glob(WINDOWS_PSQL_GLOB), key=lambda p: int(Path(p).parents[1].name))
    return candidates[-1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--superuser", default="postgres", help="Postgres superuser to connect as")
    parser.add_argument("--psql", default=None, help="Path to psql (auto-detected if omitted)")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop and recreate the application database (asks you to confirm)",
    )
    args = parser.parse_args()

    psql = args.psql or find_psql()
    if psql is None:
        print("psql not found — pass --psql <path-to-psql>", file=sys.stderr)
        return 1

    settings = get_settings()
    if args.reset:
        typed = input(
            f"This deletes the database '{settings.postgres_db}' and every record in it. "
            "Type its name to confirm: "
        )
        if typed.strip() != settings.postgres_db:
            print("Not confirmed — nothing was changed.", file=sys.stderr)
            return 1
    command = [
        psql,
        "--host", settings.postgres_host,
        "--port", str(settings.postgres_port),
        "--username", args.superuser,
        "--dbname", "postgres",
        "--set", f"app_user={settings.postgres_user}",
        "--set", f"app_password={settings.postgres_password.get_secret_value()}",
        "--set", f"app_db={settings.postgres_db}",
        "--set", f"app_test_db={settings.postgres_test_db}",
        "--set", f"reset={'true' if args.reset else 'false'}",
        "--file", str(SQL_FILE),
    ]  # fmt: skip
    result = subprocess.run(command, check=False)
    if result.returncode == 0:
        print(
            f"OK: role '{settings.postgres_user}', databases '{settings.postgres_db}' and "
            f"'{settings.postgres_test_db}' ready."
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
