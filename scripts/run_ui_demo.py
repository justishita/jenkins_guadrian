"""Show the P3 demo in the web UI, on a laptop with no Docker.

**Owner:** P3. One command:

    python scripts/run_ui_demo.py

1. Runs ``scripts/demo_p3.py --to-ui``: all five scenarios, written to a local SQLite
   database plus a blackboard and report directory under ``data/demo_ui/``.
2. Starts the API (``backend.main``) on http://127.0.0.1:8000 against that data.
3. Starts the UI dev server (``ui/``, Vite) on http://localhost:5173, which proxies
   ``/api`` to the API. Pass ``--no-ui`` to start only the API.

The UI login is seeded from ``.env.demo`` (git-ignored), which this script creates on
first run with a random password and session secret. Open that file for the login.

RabbitMQ is not needed: the API retries it in the background and a decision made in
the UI is still recorded; only the ``approval.decided`` message is not delivered.
Ctrl+C stops everything.

With the full Docker stack running instead (``docker compose up -d``):

    python scripts/run_ui_demo.py --docker

adds the five demo incidents to the stack's own database, blackboard and reports
(``data/``), next to any real incidents, and starts nothing. Open the stack's UI on
http://localhost:3001. It needs Postgres published on 127.0.0.1:55432, which the local,
git-ignored ``docker-compose.override.yml`` does.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = REPO_ROOT / "data" / "demo_ui"
ENV_FILE = REPO_ROOT / ".env.demo"
ADMIN_EMAIL = "admin@jenkinsguardians.test"


def demo_settings() -> dict[str, str]:
    """Read ``.env.demo``, creating it with fresh random secrets the first time."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(
            "# Local UI demo only (scripts/run_ui_demo.py). Git-ignored; never use these elsewhere.\n"
            f"UI_ADMIN_EMAIL={ADMIN_EMAIL}\n"
            f"UI_ADMIN_PASSWORD={secrets.token_urlsafe(12)}\n"
            f"UI_SESSION_SECRET={secrets.token_urlsafe(48)}\n"
            f"APPROVAL_API_TOKEN={secrets.token_urlsafe(32)}\n",
            encoding="utf-8",
        )
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def environment() -> dict[str, str]:
    env = dict(os.environ)
    env.update(demo_settings())
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{(DEMO_ROOT / 'demo.db').as_posix()}",
            "BLACKBOARD_DIR": str(DEMO_ROOT / "blackboard"),
            "REPORTS_DIR": str(DEMO_ROOT / "reports"),
            "CATCHUP_ENABLED": "false",
            # Nothing listens here, so connection attempts fail fast instead of hanging.
            "RABBITMQ_URL": "amqp://guest:guest@127.0.0.1:5672/",
            "UI_BCRYPT_ROUNDS": "10",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def into_docker_stack() -> int:
    """Write the demo incidents into the running Docker stack's database and data/."""
    settings = dotenv(REPO_ROOT / ".env")
    missing = [key for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not settings.get(key)]
    if missing:
        print(f".env is missing {', '.join(missing)}", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:55432/{settings['POSTGRES_DB']}"
    )
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "demo_p3.py"), "--to-ui", "--ui-root", str(REPO_ROOT / "data")],
        cwd=REPO_ROOT,
        env=env,
    )
    if result.returncode == 0:
        print("
  Demo incidents added. Open http://localhost:3001 (login: UI_ADMIN_* in .env).
")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the P3 demo and show it in the web UI.")
    parser.add_argument("--no-ui", action="store_true", help="start only the API, not the UI dev server")
    parser.add_argument("--skip-demo", action="store_true", help="keep the existing demo data")
    parser.add_argument("--docker", action="store_true", help="add the demo to the running Docker stack instead")
    args = parser.parse_args()
    if args.docker:
        return into_docker_stack()
    env = environment()

    if not args.skip_demo:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "demo_p3.py"), "--to-ui"], cwd=REPO_ROOT, env=env
        )
        if result.returncode != 0:
            print("The demo failed; not starting the UI.", file=sys.stderr)
            return result.returncode

    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=REPO_ROOT,
            env=env,
        )
    ]
    if not args.no_ui:
        npm = shutil.which("npm")
        if npm is None:
            print("npm not found; starting only the API. Install Node.js to run the UI.", file=sys.stderr)
        else:
            ui = REPO_ROOT / "ui"
            if not (ui / "node_modules").is_dir():
                subprocess.run([npm, "ci"], cwd=ui, check=True)
            processes.append(subprocess.Popen([npm, "run", "dev"], cwd=ui, env=env))

    print("\n  API  http://127.0.0.1:8000/docs")
    if len(processes) > 1:
        print("  UI   http://localhost:5173   (login: see .env.demo)")
    print("  Ctrl+C to stop.\n")
    try:
        for process in processes:
            process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
