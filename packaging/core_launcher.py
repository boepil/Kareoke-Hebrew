"""Launch the Karaoke Studio Core edition in a local native web window."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[1]


def default_user_data_dir() -> Path:
    override = os.environ.get("KARAOKE_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "KaraokeStudio" / "CoreData").resolve()
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return (Path(local_app_data) / "KaraokeStudio" / "CoreData").resolve()


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_server(url: str, timeout_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return 200 <= int(response.status) < 500
        except Exception:
            time.sleep(0.15)
    return False


def main() -> int:
    root = bundle_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    user_data = default_user_data_dir()
    user_data.mkdir(parents=True, exist_ok=True)

    os.environ["KARAOKE_CORE_EDITION"] = "1"
    os.environ["KARAOKE_USER_DATA_DIR"] = str(user_data)
    os.environ["KARAOKE_APP_ROOT"] = str(root)

    # Prepend runtime and standard CLI locations to PATH
    extra_paths = [
        str(root),
        str(Path(sys.executable).resolve().parent),
    ]
    if sys.platform == "darwin":
        resources_dir = Path(sys.executable).resolve().parent.parent / "Resources"
        if resources_dir.is_dir():
            extra_paths.append(str(resources_dir))
        extra_paths.extend(["/opt/homebrew/bin", "/usr/local/bin"])

    existing_path = os.environ.get("PATH", "")
    new_path_entries = [p for p in extra_paths if p and Path(p).is_dir()]
    os.environ["PATH"] = os.pathsep.join(new_path_entries + ([existing_path] if existing_path else []))

    from timing_editor import create_app

    config_path = root / "config.yaml"
    app = create_app(config_path)
    port = free_local_port()
    url = f"http://127.0.0.1:{port}/"
    server = threading.Thread(
        target=app.run,
        kwargs={
            "host": "127.0.0.1",
            "port": port,
            "debug": False,
            "use_reloader": False,
            "threaded": True,
        },
        daemon=True,
    )
    server.start()

    if not wait_for_server(url):
        raise RuntimeError("Karaoke Studio Core could not start its local editor server.")

    try:
        import webview  # type: ignore
    except ImportError:
        webbrowser.open(url)
        while server.is_alive():
            time.sleep(1.0)
        return 0

    window = webview.create_window(
        "Karaoke Studio Core",
        url,
        width=1440,
        height=1000,
        min_size=(1100, 700),
        resizable=True,
    )
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
