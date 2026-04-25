import re

import psutil

from agent._shell import run_safe

# Only allow safe service/unit name characters
_SERVICE_RE = re.compile(r'^[a-zA-Z0-9@_.\-:]{1,64}$')
_LOGS_MAX_LINES = 500


def _validate_service_name(name) -> str | None:
    """Return error string if name is invalid, else None."""
    if not isinstance(name, str) or not name.strip():
        return 'service name must be a non-empty string'
    if not _SERVICE_RE.match(name.strip()):
        return 'service name contains invalid characters (allowed: a-z A-Z 0-9 @ _ . - :)'
    return None


def service_status(name: str) -> dict:
    """Query systemd service status via systemctl."""
    err = _validate_service_name(name)
    if err:
        return {'ok': False, 'error': err}
    name = name.strip()
    result = run_safe(['systemctl', 'status', '--no-pager', '-n', '0', name])
    return {**result, 'service': name}


def service_logs(name: str, lines: int = 50) -> dict:
    """Fetch recent journal entries for a systemd service."""
    err = _validate_service_name(name)
    if err:
        return {'ok': False, 'error': err}
    if not isinstance(lines, int) or lines < 1:
        lines = 50
    lines = min(lines, _LOGS_MAX_LINES)
    name = name.strip()
    result = run_safe(['journalctl', '-u', name, '-n', str(lines), '--no-pager', '--output=short'])
    return {**result, 'service': name, 'lines': lines}


def list_services(pattern: str = '') -> dict:
    """List systemd services, optionally filtered by a substring pattern."""
    result = run_safe(['systemctl', 'list-units', '--type=service', '--no-pager', '--plain', '--all'])
    if not result['ok']:
        return result
    # pattern matching is done in Python — never passed to shell
    pat = (pattern or '').strip().lower()
    services = []
    for line in result['stdout'].splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.endswith('.service'):
            continue
        if pat and pat not in unit.lower():
            continue
        status = parts[2] if len(parts) > 2 else ''
        sub = parts[3] if len(parts) > 3 else ''
        services.append({'unit': unit, 'load': parts[1] if len(parts) > 1 else '', 'active': status, 'sub': sub})
    return {'ok': True, 'count': len(services), 'services': services}


def port_listen(port: int = None) -> dict:
    """List listening TCP/UDP ports, optionally filtered by port number."""
    if port is not None and (not isinstance(port, int) or port < 1 or port > 65535):
        return {'ok': False, 'error': 'port must be an integer between 1 and 65535'}
    result = run_safe(['ss', '-tlnpu'])
    if not result['ok']:
        return result
    # parse and filter in Python
    lines = result['stdout'].splitlines()
    if port is not None:
        lines = [l for l in lines if f':{port}' in l or f':{port} ' in l]
    return {'ok': True, 'output': '\n'.join(lines)}


def disk_usage() -> dict:
    """Report disk usage excluding tmpfs/devtmpfs."""
    result = run_safe(['df', '-h', '-x', 'tmpfs', '-x', 'devtmpfs'])
    return result


def system_info() -> dict:
    """Return basic system info: uptime, CPU, memory, kernel."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        boot = psutil.boot_time()
        import time
        uptime_s = int(time.time() - boot)
        hours, rem = divmod(uptime_s, 3600)
        minutes = rem // 60
        uname_result = run_safe(['uname', '-a'])
        return {
            'ok': True,
            'uptime': f'{hours}h {minutes}m',
            'cpu_percent': cpu_pct,
            'mem_total_gb': round(mem.total / 1024 ** 3, 2),
            'mem_used_gb': round(mem.used / 1024 ** 3, 2),
            'mem_percent': mem.percent,
            'kernel': uname_result.get('stdout', '').strip(),
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}
