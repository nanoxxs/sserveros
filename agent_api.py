#!/usr/bin/env python3
"""Token-authenticated HTTP API used by a sserveros controller."""

import os

from storage import config_path, ensure_runtime_dir, load_config_file, runtime_path
from webui import create_app


class AgentPrefixMiddleware:
    """Expose the local API only below /agent/api/v1 on the Agent port."""

    prefix = '/agent/api/v1'

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.prefix or path.startswith(self.prefix + '/'):
            suffix = path[len(self.prefix):]
            environ['PATH_INFO'] = '/api/health' if suffix in ('', '/') else '/api' + suffix
            environ['SSERVEROS_AGENT_REQUEST'] = '1'
        return self.app(environ, start_response)


def create_agent_app(script_dir: str = None):
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    app = create_app(
        script_dir=script_dir,
        start_background=False,
        agent_api_only=True,
    )
    app.wsgi_app = AgentPrefixMiddleware(app.wsgi_app)
    return app


def _write_pid(script_dir: str) -> str:
    ensure_runtime_dir(script_dir)
    path = runtime_path(script_dir, 'agent_api.pid')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(f'{os.getpid()}\n')
    os.replace(tmp, path)
    return path


def _cleanup_pid(path: str):
    try:
        with open(path, encoding='utf-8') as f:
            recorded = int(f.read().strip())
        if recorded == os.getpid():
            os.remove(path)
    except (OSError, ValueError):
        pass


if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    app = create_agent_app(script_dir)
    cfg = load_config_file(config_path(script_dir))
    host = str(cfg.get('agent_host', '0.0.0.0'))
    port = int(cfg.get('agent_port', 6780))
    pid_path = _write_pid(script_dir)
    print(
        f'[sserveros agent] listening on http://{host}:{port}/agent/api/v1',
        flush=True,
    )
    try:
        app.run(host=host, port=port, debug=False, load_dotenv=False)
    finally:
        _cleanup_pid(pid_path)
