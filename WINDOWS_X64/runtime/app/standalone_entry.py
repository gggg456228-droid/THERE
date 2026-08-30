import atexit
import os
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
LINK_TEXT = "https://github.com/gggg456228-droid/THERE"

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
    x = 0
    y = 6
    dx = 1
    dy = 1
    previous = None

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

            x = min(max(0, x), max_x)
            y = min(max(min_y, y), max_y)

            if previous is not None:
                move_cursor(previous[0], previous[1])
                kernel32.SetConsoleTextAttribute(handle, 7)
                sys.stdout.write(" " * len(LINK_TEXT))

            move_cursor(x, y)
            kernel32.SetConsoleTextAttribute(handle, colors[color_index])
            sys.stdout.write(LINK_TEXT)
            sys.stdout.flush()
            previous = (x, y)

            next_x = x + dx
            next_y = y + dy
            bounced = False

            if next_x < 0 or next_x > max_x:
                dx *= -1
                next_x = x + dx
                bounced = True

            if next_y < min_y or next_y > max_y:
                dy *= -1
                next_y = y + dy
                bounced = True

            if bounced:
                color_index = (color_index + 1) % len(colors)

            x = next_x
            y = next_y
            _stop_event.wait(0.065)
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
