from flask import request

def init_security_system(): return True
def check_security_before_request(is_privileged=False): return True, None, None
def log_action(*a, **kw): return None
def get_client_ip(): return request.remote_addr or "127.0.0.1"
def is_ip_blocked(ip): return False
def block_ip(*a, **kw): return False
def check_login_throttle(*a, **kw): return True, ""
def load_logs(): return []
def load_blocked_ips(): return {}
def save_blocked_ips(data): return True
