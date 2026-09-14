import atexit
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile

APP_VERSION = "2.6.9"
HOST = "127.0.0.1"
LINK_TEXT = "https://t.me/pluf255"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/gggg456228-droid/THERE/main/update/latest.json"
UPDATE_USER_AGENT = f"THERE-Updater/{APP_VERSION}"

_stop_event = threading.Event()
_server = None
_server_thread = None


def _version_tuple(value):
    clean = str(value or "").strip().split("-", 1)[0].split("+", 1)[0]
    out = []
    for item in clean.split("."):
        try:
            out.append(int(item))
        except Exception:
            out.append(0)
    return tuple(out)


def _executable_path():
    try:
        p = Path(sys.argv[0]).resolve()
        if p.suffix.lower() == ".exe":
            return p
    except Exception:
        pass
    try:
        p = Path(sys.executable).resolve()
        if p.suffix.lower() == ".exe":
            return p
    except Exception:
        pass
    return Path(sys.argv[0]).resolve()


def _installation_root():
    return _executable_path().parent


def _read_local_version(root):
    try:
        value = (root / "VERSION.txt").read_text(encoding="utf-8").strip()
        if value:
            return value
    except Exception:
        pass
    return APP_VERSION


def _download_bytes(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": UPDATE_USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        status = getattr(r, "status", 200)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Файл обновления слишком большой")
    return data


def _safe_extract_zip(archive_path, target_dir):
    target_dir = Path(target_dir).resolve()
    with zipfile.ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            target = (target_dir / info.filename).resolve()
            if target != target_dir and target_dir not in target.parents:
                raise RuntimeError("Небезопасный путь в архиве обновления")
        archive.extractall(target_dir)


def _schedule_update(new_exe, temp_root):
    if os.name != "nt":
        return False
    current_exe = _executable_path()
    pid = os.getpid()
    helper = Path(tempfile.gettempdir()) / f"there-update-{pid}.cmd"
    lines = [
        "@echo off",
        "setlocal",
        f'set "CURPID={pid}"',
        ":wait_current",
        'tasklist /FI "PID eq %CURPID%" 2>NUL | find "%CURPID%" >NUL',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >NUL",
        "  goto wait_current",
        ")",
        ":copy_retry",
        f'copy /Y "{new_exe}" "{current_exe}" >NUL 2>NUL',
        "if errorlevel 1 (",
        "  timeout /t 1 /nobreak >NUL",
        "  goto copy_retry",
        ")",
        f'start "" "{current_exe}" --skip-update-once',
        f'rmdir /S /Q "{temp_root}" 2>NUL',
        'del "%~f0"',
    ]
    helper.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    subprocess.Popen(
        ["cmd.exe", "/C", "start", "", "/min", str(helper)],
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def maybe_update_from_github():
    if os.name != "nt" or os.environ.get("THERE_DISABLE_UPDATE") == "1":
        return False
    if "--skip-update-once" in sys.argv:
        return False

    root = _installation_root()
    local_version = _read_local_version(root)
    temp_root = None
    try:
        manifest = json.loads(_download_bytes(UPDATE_MANIFEST_URL, 64 * 1024).decode("utf-8-sig"))
        remote_version = str(manifest.get("version") or "").strip()
        bundle_url = str(manifest.get("windows_bundle_url") or "").strip()
        expected_sha = str(manifest.get("sha256") or "").strip().lower()
        if not remote_version or not bundle_url:
            return False
        if _version_tuple(remote_version) <= _version_tuple(local_version):
            return False

        print(f"Найдено обновление THERE {local_version} -> {remote_version}. Загружаю...")
        temp_root = Path(tempfile.mkdtemp(prefix="there-update-"))
        archive_path = temp_root / "update.zip"
        extracted = temp_root / "new"
        payload = _download_bytes(bundle_url, 300 * 1024 * 1024)
        archive_path.write_bytes(payload)
        if expected_sha and hashlib.sha256(payload).hexdigest().lower() != expected_sha:
            raise RuntimeError("SHA256 обновления не совпал")
        extracted.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(archive_path, extracted)
        new_exe = extracted / "THERE.exe"
        if not new_exe.is_file():
            raise RuntimeError("В пакете обновления отсутствует THERE.exe")
        if _schedule_update(new_exe, temp_root):
            print("Обновление загружено. THERE перезапустится автоматически.")
            return True
    except Exception as exc:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
        print(f"Проверка обновлений не удалась, запускаю текущую версию: {exc}")
    return False


def data_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.environ.get("THERE_DATA_DIR") or os.path.join(base, "THERE", "data")
    os.makedirs(path, exist_ok=True)
    os.environ["THERE_DATA_DIR"] = path
    return path


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def server_is_alive(url, timeout=0.5):
    try:
        with urllib.request.urlopen(url + "healthz", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def wait_until_alive(url, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline and not _stop_event.is_set():
        if server_is_alive(url):
            return True
        if _server_thread is not None and not _server_thread.is_alive():
            return False
        time.sleep(0.1)
    return False


def open_site(url):
    if os.environ.get("THERE_NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass


def stop_server():
    global _server
    _stop_event.set()
    server = _server
    _server = None
    if server is not None:
        try:
            server.close()
        except Exception:
            pass


def _console_api():
    if os.name != "nt" or not sys.stdout.isatty():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return None

        class COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("bVisible", wintypes.BOOL)]

        return kernel32, handle, COORD, CURSORINFO
    except Exception:
        return None


def _run_console_animation(url):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"THERE {APP_VERSION}")
        except Exception:
            pass
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    print(f"THERE {APP_VERSION}")
    print(f"Сайт открыт по адресу: {url}")
    print("Закройте это окно или нажмите Ctrl+C, чтобы остановить THERE.")
    print()

    api = _console_api()
    if api is None:
        print(LINK_TEXT)
        while not _stop_event.wait(0.25):
            if _server_thread is not None and not _server_thread.is_alive():
                break
        return

    kernel32, handle, COORD, cursor_info_type = api
    colors = [9, 10, 11, 12, 13, 14, 15]
    rng = random.SystemRandom()
    x, y = 0.0, 5.0
    vx, vy = 1.0, 0.63
    previous = None
    color_index = 0

    def move(px, py):
        kernel32.SetConsoleCursorPosition(handle, COORD(int(px), int(py)))

    try:
        while not _stop_event.is_set():
            if _server_thread is not None and not _server_thread.is_alive():
                break
            size = shutil.get_terminal_size((80, 25))
            max_x = max(0, size.columns - len(LINK_TEXT) - 1)
            min_y = 5
            max_y = max(min_y, size.lines - 2)
            x = min(max(0.0, x), float(max_x))
            y = min(max(float(min_y), y), float(max_y))
            dx, dy = int(round(x)), int(round(y))
            if previous is not None and previous != (dx, dy):
                move(*previous)
                kernel32.SetConsoleTextAttribute(handle, 7)
                sys.stdout.write(" " * len(LINK_TEXT))
            move(dx, dy)
            kernel32.SetConsoleTextAttribute(handle, colors[color_index])
            sys.stdout.write(LINK_TEXT)
            sys.stdout.flush()
            previous = (dx, dy)

            nx, ny = x + vx, y + vy
            hit_x = nx < 0 or nx > max_x
            hit_y = ny < min_y or ny > max_y
            if hit_x:
                vx = (-1 if vx > 0 else 1) * rng.uniform(0.72, 1.38)
                color_index = (color_index + 1) % len(colors)
            if hit_y:
                vy = (-1 if vy > 0 else 1) * rng.uniform(0.38, 1.12)
                color_index = (color_index + 1) % len(colors)
            if hit_x or hit_y:
                if abs(vx) < abs(vy) * 0.55:
                    vx = (1 if vx >= 0 else -1) * abs(vy) * rng.uniform(0.65, 1.25)
                if abs(vy) < abs(vx) * 0.35:
                    vy = (1 if vy >= 0 else -1) * abs(vx) * rng.uniform(0.45, 0.95)
                nx = min(max(0.0, x + vx), float(max_x))
                ny = min(max(float(min_y), y + vy), float(max_y))
            x, y = nx, ny
            _stop_event.wait(0.055)
    finally:
        try:
            kernel32.SetConsoleTextAttribute(handle, 7)
        except Exception:
            pass


def _start_server(port):
    global _server, _server_thread
    from flask_server import app
    from waitress.server import create_server

    _server = create_server(
        app,
        host=HOST,
        port=port,
        threads=16,
        channel_timeout=60,
        cleanup_interval=15,
    )
    _server_thread = threading.Thread(target=_server.run, name="THERE Local Server", daemon=True)
    _server_thread.start()


def main():
    self_test = "--self-test" in sys.argv
    if self_test:
        os.environ["THERE_DISABLE_UPDATE"] = "1"
        os.environ["THERE_NO_BROWSER"] = "1"

    if maybe_update_from_github():
        return 0

    data_dir()
    atexit.register(stop_server)
    port = free_port()
    os.environ["THERE_PORT"] = str(port)
    url = f"http://{HOST}:{port}/"
    _start_server(port)

    if not wait_until_alive(url):
        print("Сервер THERE не успел запуститься.")
        stop_server()
        return 1

    if self_test:
        print(f"THERE_SELF_TEST_OK {url}")
        stop_server()
        return 0

    open_site(url)
    try:
        _run_console_animation(url)
    except KeyboardInterrupt:
        _stop_event.set()
    finally:
        stop_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
