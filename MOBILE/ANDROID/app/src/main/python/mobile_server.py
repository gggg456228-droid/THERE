import os
import socket
import threading

_server = None
_thread = None
_lock = threading.RLock()
_url = None


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def start_server(data_dir):
    global _server, _thread, _url
    with _lock:
        if _server is not None and _url:
            return _url
        port = _free_port()
        _url = f"http://127.0.0.1:{port}/"
        os.makedirs(data_dir, exist_ok=True)
        os.environ["THERE_DATA_DIR"] = data_dir
        os.environ["THERE_PORT"] = str(port)
        from flask_server import app
        from werkzeug.serving import make_server
        _server = make_server("127.0.0.1", port, app, threaded=True)
        _thread = threading.Thread(target=_server.serve_forever, name="THERE-local-server", daemon=True)
        _thread.start()
        return _url


def stop_server():
    global _server, _thread, _url
    with _lock:
        server = _server
        thread = _thread
        _server = None
        _thread = None
        _url = None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    return True


def is_running():
    return _server is not None


def current_url():
    return _url or ""
