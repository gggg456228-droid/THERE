import atexit
import os
import random
from pathlib import Path
import zipfile
import tempfile
import json
import hashlib
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

PORT = 33851
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}/"
LINK_TEXT = "https://t.me/pluf255"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/gggg456228-droid/THERE/main/update/latest.json"
UPDATE_USER_AGENT = "THERE-Python-Updater/2"


def _version_tuple(value):
    clean = str(value or "").strip().split("-", 1)[0].split("+", 1)[0]
    parts = []
    for item in clean.split("."):
        try:
            parts.append(int(item))
        except Exception:
            parts.append(0)
    return tuple(parts)


def _installation_root():
    here = Path(__file__).resolve()
    candidates = [here.parents[2], Path.cwd()]
    for root in candidates:
        if (root / "VERSION.txt").is_file():
            return root
    return here.parents[2]


def _read_local_version(root):
    try:
        return (root / "VERSION.txt").read_text(encoding="utf-8").strip() or "0"
    except Exception:
        return "0"


def _download_bytes(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": UPDATE_USER_AGENT})
    with urllib.request.urlopen(request, timeout=12) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(response, 'status', '?')}")
        data = response.read(limit + 1)
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


def _schedule_python_update(root, extracted, temp_root):
    if os.name != "nt":
        return False
    helper = Path(tempfile.gettempdir()) / f"there-update-{os.getpid()}.cmd"
    exe = Path(root) / "THERE.exe"
    current_pid = os.getpid()
    parent_pid = os.getppid()
    lines = [
        "@echo off",
        "setlocal",
        f'set "PY_PID={current_pid}"',
        f'set "PARENT_PID={parent_pid}"',
        ":wait_python",
        'tasklist /FI "PID eq %PY_PID%" 2>NUL | find "%PY_PID%" >NUL',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >NUL",
        "  goto wait_python",
        ")",
        ":wait_parent",
        'tasklist /FI "PID eq %PARENT_PID%" 2>NUL | find "%PARENT_PID%" >NUL',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >NUL",
        "  goto wait_parent",
        ")",
        f'robocopy "{extracted}" "{root}" /E /COPY:DAT /R:3 /W:1 >NUL',
        f'start "" "{exe}" --skip-update-once',
        f'rmdir /S /Q "{temp_root}" 2>NUL',
        'del "%~f0"',
    ]
    helper.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    import subprocess
    subprocess.Popen(["cmd.exe", "/C", "start", "", "/min", str(helper)], close_fds=True)
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
        manifest = json.loads(_download_bytes(UPDATE_MANIFEST_URL, 64 * 1024).decode("utf-8"))
        remote_version = str(manifest.get("version") or "").strip()
        bundle_url = str(manifest.get("windows_bundle_url") or "").strip()
        expected_sha = str(manifest.get("sha256") or "").strip().lower()
        if not remote_version or not bundle_url or _version_tuple(remote_version) <= _version_tuple(local_version):
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
        if not (extracted / "THERE.exe").is_file() or not (extracted / "runtime" / "app" / "standalone_entry.py").is_file():
            raise RuntimeError("Пакет обновления неполный")
        if _schedule_python_update(root, extracted, temp_root):
            print("Обновление загружено. THERE сейчас перезапустится.")
            return True
    except Exception as exc:
        if temp_root is not None:
            try:
                shutil.rmtree(temp_root, ignore_errors=True)
            except Exception:
                pass
        print(f"Проверка обновлений не удалась, запускаю текущую версию: {exc}")
    return False


_stop_event = threading.Event()
_server = None
_server_thread = None
_ctrl_handler_ref = None


def data_dir():
    path = os.environ.get("THERE_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "THERE", "data"
    )
    os.makedirs(path, exist_ok=True)
    os.environ["THERE_DATA_DIR"] = path
    return path


def server_is_alive(timeout=0.5):
    try:
        with urllib.request.urlopen(URL + "healthz", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def port_is_busy():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.35)
    try:
        return sock.connect_ex((HOST, PORT)) == 0
    finally:
        sock.close()


def wait_until_alive(seconds=15.0):
    deadline = time.time() + seconds
    while time.time() < deadline and not _stop_event.is_set():
        if server_is_alive():
            return True
        time.sleep(0.1)
    return False


def open_site():
    if os.environ.get("THERE_NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(URL)
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


def _install_windows_ctrl_handler():
    global _ctrl_handler_ref
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        @handler_type
        def handler(ctrl_type):
            if ctrl_type in (0, 1, 2, 5, 6):
                _stop_event.set()
                return True
            return False

        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
        _ctrl_handler_ref = handler
    except Exception:
        _ctrl_handler_ref = None


def _windows_console_api():
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

        class CONSOLE_CURSOR_INFO(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("bVisible", wintypes.BOOL)]

        return kernel32, handle, COORD, CONSOLE_CURSOR_INFO
    except Exception:
        return None


def _set_console_title(title):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
            return
        except Exception:
            pass


def _clear_console():
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def _run_console_animation():
    api = _windows_console_api()
    _set_console_title("THERE")
    _clear_console()

    print("THERE")
    print(f"Сайт открывается по адресу: {URL}")
    print("Пока это окно открыто, сайт работает.")
    print("Закройте окно или нажмите Ctrl+C, чтобы остановить сайт.")
    print()

    if api is None:
        print(LINK_TEXT)
        while not _stop_event.wait(0.25):
            if _server_thread is not None and not _server_thread.is_alive():
                break
        return

    kernel32, handle, COORD, cursor_info_type = api
    try:
        cursor_info = cursor_info_type()
        if kernel32.GetConsoleCursorInfo(handle, cursor_info):
            cursor_info.bVisible = False
            kernel32.SetConsoleCursorInfo(handle, cursor_info)
    except Exception:
        cursor_info = None

    colors = [9, 10, 11, 12, 13, 14]
    color_index = 0
    x = 0.0
    y = 6.0
    rng = random.SystemRandom()
    vx = 1.0
    vy = 0.63
    previous = None

    def choose_velocity(hit_x=False, hit_y=False):
        nonlocal vx, vy

        # После каждого удара меняем угол и скорость. Поэтому ссылка не
        # застревает на одной диагонали и со временем обходит всё окно.
        x_speed = rng.uniform(0.72, 1.38)
        y_speed = rng.uniform(0.38, 1.12)

        if hit_x:
            vx = x_speed if vx < 0 else -x_speed
        else:
            vx = x_speed if rng.random() < 0.5 else -x_speed

        if hit_y:
            vy = y_speed if vy < 0 else -y_speed
        else:
            vy = y_speed if rng.random() < 0.5 else -y_speed

        # Не даём траектории стать почти горизонтальной или вертикальной.
        if abs(vx / vy) < 0.55:
            vx = (1 if vx >= 0 else -1) * abs(vy) * rng.uniform(0.65, 1.25)
        elif abs(vy / vx) < 0.35:
            vy = (1 if vy >= 0 else -1) * abs(vx) * rng.uniform(0.45, 0.95)

    def move_cursor(px, py):
        kernel32.SetConsoleCursorPosition(handle, COORD(int(px), int(py)))

    try:
        while not _stop_event.is_set():
            if _server_thread is not None and not _server_thread.is_alive():
                break

            size = shutil.get_terminal_size((80, 25))
            max_x = max(0, size.columns - len(LINK_TEXT) - 1)
            min_y = 6
            max_y = max(min_y, size.lines - 2)

            x = min(max(0.0, x), float(max_x))
            y = min(max(float(min_y), y), float(max_y))

            draw_x = int(round(x))
            draw_y = int(round(y))

            if previous is not None and previous != (draw_x, draw_y):
                move_cursor(previous[0], previous[1])
                kernel32.SetConsoleTextAttribute(handle, 7)
                sys.stdout.write(" " * len(LINK_TEXT))

            move_cursor(draw_x, draw_y)
            kernel32.SetConsoleTextAttribute(handle, colors[color_index])
            sys.stdout.write(LINK_TEXT)
            sys.stdout.flush()
            previous = (draw_x, draw_y)

            next_x = x + vx
            next_y = y + vy
            hit_x = next_x < 0 or next_x > max_x
            hit_y = next_y < min_y or next_y > max_y

            if hit_x or hit_y:
                choose_velocity(hit_x=hit_x, hit_y=hit_y)
                color_index = (color_index + 1) % len(colors)
                next_x = min(max(0.0, x + vx), float(max_x))
                next_y = min(max(float(min_y), y + vy), float(max_y))

            x = next_x
            y = next_y
            _stop_event.wait(0.055)
    finally:
        try:
            kernel32.SetConsoleTextAttribute(handle, 7)
            size = shutil.get_terminal_size((80, 25))
            move_cursor(0, max(6, size.lines - 1))
            if cursor_info is not None:
                cursor_info.bVisible = True
                kernel32.SetConsoleCursorInfo(handle, cursor_info)
        except Exception:
            pass


def _start_server():
    global _server, _server_thread
    from flask_server import app
    from waitress.server import create_server

    _server = create_server(
        app,
        host=HOST,
        port=PORT,
        threads=16,
        channel_timeout=60,
        cleanup_interval=15,
    )
    _server_thread = threading.Thread(
        target=_server.run,
        name="THERE Local Server",
        daemon=True,
    )
    _server_thread.start()


def main():
    if maybe_update_from_github():
        return 0

    data_dir()
    os.environ["THERE_PORT"] = str(PORT)
    _install_windows_ctrl_handler()
    atexit.register(stop_server)

    if server_is_alive():
        open_site()
        return 0

    if port_is_busy():
        print(f"Порт {PORT} уже занят другой программой.")
        print("Закройте программу, которая заняла порт, и запустите THERE снова.")
        if sys.stdin.isatty():
            input("Нажмите Enter для выхода.")
        return 1

    _start_server()

    if not wait_until_alive():
        print("Сервер THERE не успел запуститься.")
        stop_server()
        if sys.stdin.isatty():
            input("Нажмите Enter для выхода.")
        return 1

    open_site()

    try:
        _run_console_animation()
    except KeyboardInterrupt:
        _stop_event.set()
    finally:
        stop_server()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
