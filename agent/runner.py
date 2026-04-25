import json
import os
import threading
import time
import uuid

import httpx

from agent.schema import TOOL_SCHEMAS, SYSTEM_PROMPT
from agent.tools import READ_ONLY_TOOLS, WRITE_TOOLS
from storage import atomic_write_json, runtime_path

_SESSION_TTL = 1800  # 30 minutes


class Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.messages: list = []
        self.pending_actions: list = []
        self.updated_at: float = time.time()

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'messages': self.messages,
            'pending_actions': self.pending_actions,
            'updated_at': self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'Session':
        s = cls(d['session_id'])
        s.messages = d.get('messages', [])
        s.pending_actions = d.get('pending_actions', [])
        s.updated_at = d.get('updated_at', time.time())
        return s


class SessionStore:
    def __init__(self, script_dir: str):
        self._script_dir = script_dir
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._load()
        self._start_cleanup()

    def _path(self) -> str:
        return runtime_path(self._script_dir, 'agent_sessions.json')

    def _load(self):
        path = self._path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            now = time.time()
            for sid, sd in data.items():
                if now - sd.get('updated_at', 0) < _SESSION_TTL:
                    self._sessions[sid] = Session.from_dict(sd)
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self):
        try:
            data = {sid: s.to_dict() for sid, s in self._sessions.items()}
            atomic_write_json(self._path(), data)
        except Exception:
            pass

    def _start_cleanup(self):
        def _cleanup():
            while True:
                time.sleep(60)
                with self._lock:
                    now = time.time()
                    stale = [sid for sid, s in self._sessions.items()
                             if now - s.updated_at > _SESSION_TTL]
                    for sid in stale:
                        del self._sessions[sid]
                    if stale:
                        self._save()

        t = threading.Thread(target=_cleanup, daemon=True)
        t.start()

    def get(self, session_id: str) -> Session:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = Session(session_id)
            return self._sessions[session_id]

    def touch(self, session: Session):
        with self._lock:
            session.updated_at = time.time()
            self._save()

    def clear(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)
            self._save()


def _summarize_result(name: str, result: dict) -> str:
    if not result.get('ok'):
        return result.get('error', 'error')
    if name == 'search_processes':
        n = result.get('count', 0)
        return f'找到 {n} 个进程'
    if name == 'service_status':
        out = result.get('stdout', '')
        for line in out.splitlines():
            if 'Active:' in line:
                return line.strip()
        return out[:80]
    if name in ('add_watch_pid', 'remove_watch_pid'):
        return result.get('message', '已暂存')
    if name == 'list_watch_pids':
        return f"{len(result.get('watch_pids', []))} 个 PID 在监控"
    if name == 'gpu_state':
        gpus = result.get('state', {}).get('gpus', [])
        return f'{len(gpus)} 张 GPU'
    out = result.get('stdout', result.get('output', ''))
    return (out[:100] + '...') if len(out) > 100 else out


class AgentRunner:
    def __init__(self, cfg: dict, script_dir: str, session_store: SessionStore):
        self._cfg = cfg
        self._script_dir = script_dir
        self._store = session_store

    def _call_tool(self, name: str, args: dict, pending: list, counter: list) -> str:
        if name in READ_ONLY_TOOLS:
            fn = READ_ONLY_TOOLS[name]
            if name in ('list_watch_pids', 'gpu_state'):
                result = fn(self._script_dir)
            else:
                result = fn(**args)
        elif name in WRITE_TOOLS:
            fn = WRITE_TOOLS[name]
            result = fn(**args)
            if result.get('staged'):
                action_id = f'act_{counter[0]}'
                counter[0] += 1
                pending.append({'id': action_id, **result})
                result['action_id'] = action_id
        else:
            result = {'ok': False, 'error': f'unknown tool: {name}'}
        return json.dumps(result, ensure_ascii=False)

    def chat(self, session_id: str, user_message: str) -> dict:
        session = self._store.get(session_id)
        session.messages.append({'role': 'user', 'content': user_message})

        llm_messages = [{'role': 'system', 'content': SYSTEM_PROMPT}] + [
            {k: v for k, v in m.items() if k != 'staged'} for m in session.messages
        ]

        base_url = self._cfg.get('llm_base_url', 'https://api.deepseek.com/v1').rstrip('/')
        api_key = self._cfg.get('llm_api_key', '')
        model = self._cfg.get('llm_model', 'deepseek-chat')
        max_iter = max(1, min(int(self._cfg.get('llm_max_iterations', 8)), 20))
        timeout = max(5, min(int(self._cfg.get('llm_request_timeout', 30)), 120))
        temperature = max(0.0, min(float(self._cfg.get('llm_temperature', 0.2)), 2.0))

        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        tool_trace = []
        pending = []
        counter = [len(session.pending_actions)]
        final_reply = ''

        for _ in range(max_iter):
            try:
                resp = httpx.post(
                    f'{base_url}/chat/completions',
                    headers=headers,
                    json={
                        'model': model,
                        'messages': llm_messages,
                        'tools': TOOL_SCHEMAS,
                        'tool_choice': 'auto',
                        'temperature': temperature,
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.TimeoutException:
                return {'ok': False, 'error': f'LLM 请求超时（>{timeout}s）'}
            except httpx.HTTPStatusError as e:
                return {'ok': False, 'error': f'LLM 返回 HTTP {e.response.status_code}: {e.response.text[:300]}'}
            except Exception as e:
                return {'ok': False, 'error': str(e)}

            choice = data['choices'][0]
            msg = choice['message']
            llm_messages.append(msg)

            if msg.get('tool_calls'):
                for tc in msg['tool_calls']:
                    fn_name = tc['function']['name']
                    try:
                        fn_args = json.loads(tc['function']['arguments'])
                    except (json.JSONDecodeError, KeyError):
                        fn_args = {}

                    result_str = self._call_tool(fn_name, fn_args, pending, counter)
                    try:
                        result_data = json.loads(result_str)
                    except Exception:
                        result_data = {}

                    tool_trace.append({
                        'name': fn_name,
                        'args': fn_args,
                        'ok': result_data.get('ok', True),
                        'summary': _summarize_result(fn_name, result_data),
                    })

                    llm_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc['id'],
                        'content': result_str,
                    })
            else:
                final_reply = msg.get('content', '')
                break
        else:
            final_reply = '（已达到最大工具调用轮次，以上是目前的分析结果）'

        session.messages.append({'role': 'assistant', 'content': final_reply})
        session.pending_actions.extend(pending)
        self._store.touch(session)

        return {
            'ok': True,
            'reply': final_reply,
            'tool_trace': tool_trace,
            'pending_actions': pending,
        }

    def confirm(self, session_id: str, approved: list, rejected: list, exec_fn) -> dict:
        session = self._store.get(session_id)
        approved_set = set(approved)
        rejected_set = set(rejected)
        executed, skipped, remaining = [], [], []

        for action in session.pending_actions:
            aid = action.get('id')
            if aid in approved_set:
                result = exec_fn(action)
                executed.append({'id': aid, 'ok': result.get('ok', False), 'message': result.get('message', '')})
            elif aid in rejected_set:
                skipped.append(aid)
            else:
                remaining.append(action)

        session.pending_actions = remaining
        self._store.touch(session)
        return {'ok': True, 'executed': executed, 'skipped': skipped}

    def clear_session(self, session_id: str):
        self._store.clear(session_id)
