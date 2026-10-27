"""Start the Governed Text-to-SQL app and open it in your browser — one command.

    python start.py                              # opens http://localhost:8000
    python start.py --data banking --user sam    # opens on Banking, signed in as Sam
    python start.py --port 8002 --no-browser

Run it from anywhere: it switches to this folder (so .env and .password.env
are used), uses the project's .venv if there is one, picks the next free
port if 8000 is taken by another program, and just opens the page if the
app is already running. Press Ctrl+C to stop the app.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def use_project_venv() -> None:
    """Re-run this script with the project's .venv Python when started with another one."""
    py = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if py.exists() and Path(sys.prefix).resolve() != VENV.resolve() and not os.environ.get("START_PY_REEXEC"):
        os.environ["START_PY_REEXEC"] = "1"
        os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def app_is_running(port: int) -> bool:
    """Is *this* app answering on the port? (Its /health returns {"status": "ok"}.)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def page_url(port: int, data: str | None, user: str | None) -> str:
    query = urllib.parse.urlencode({k: v for k, v in (("data", data), ("user", user)) if v})
    return f"http://localhost:{port}/" + (f"?{query}" if query else "")


def open_when_ready(port: int, url: str, browser: bool) -> None:
    for _ in range(120):  # up to a minute: the first start can be slow
        if app_is_running(port):
            print(f"\n  Open: {url}\n  Stop: press Ctrl+C in this window\n", flush=True)
            if browser:
                webbrowser.open(url)
            return
        time.sleep(0.5)
    print("\n  The app did not answer within a minute — see the messages above.\n", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the Text-to-SQL app and open it in the browser.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)), help="port to use (default 8000)")
    ap.add_argument("--data", help="open on this data, e.g. banking or retail")
    ap.add_argument("--user", help="open signed in as this demo user, e.g. sam or priya")
    ap.add_argument("--no-browser", action="store_true", help="start the app but do not open a browser")
    args = ap.parse_args()

    use_project_venv()
    os.chdir(ROOT)  # .env and .password.env are read from the current folder

    port = args.port
    if app_is_running(port):
        url = page_url(port, args.data, args.user)
        print(f"The app is already running on port {port}. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    while not port_is_free(port):
        print(f"Port {port} is used by another program; trying {port + 1}.")
        port += 1
        if port > args.port + 20:
            print("No free port found. Stop the other program or pass --port.", file=sys.stderr)
            return 1

    try:
        import uvicorn

        from sql_agent.config import get_settings
    except ImportError as e:
        print(f"Missing package ({e.name}). Install the project first:  python -m pip install -e .", file=sys.stderr)
        return 1
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set. Add it to .env (see .env.example), then run this again.", file=sys.stderr)
        return 1

    url = page_url(port, args.data, args.user)
    print(f"Starting the app on port {port} (model: {settings.model_backend}) …", flush=True)
    threading.Thread(target=open_when_ready, args=(port, url, not args.no_browser), daemon=True).start()
    try:
        uvicorn.run("sql_agent.api:app", host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
