"""Persistent first-run progress and explicit, primary-only model connection test."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import httpx
from .http import make_client

DEEPSEEK_BASE = 'https://api.deepseek.com'
DEEPSEEK_MODEL = 'deepseek-flash'


def model_fingerprint(llm) -> str:
    values = [str(llm.primary_base or '').rstrip('/'), llm.primary_model, llm.primary_key]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class OnboardingStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, cfg) -> dict:
        # Existing configured installations should not be forced through a new wizard.
        legacy = bool(cfg.llm.primary_key and (cfg.adapter.wechat_enabled or cfg.adapter.qq_enabled))
        state = {'version': 1, 'step': 1, 'completed': legacy, 'dismissed': False,
                 'model_receipt': ''}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(saved, dict):
                    state.update({k: saved[k] for k in state if k in saved})
            except (ValueError, OSError):
                # Corrupt progress never removes the user's actual settings.
                state['completed'] = False
        step = state.get('step')
        state['step'] = step if type(step) is int and 1 <= step <= 5 else 1
        for key in ('completed', 'dismissed'):
            state[key] = state[key] is True
        state['version'] = 1
        return state

    def save(self, state: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def public(self, cfg, wechat_logged_in=False) -> dict:
        state = self.load(cfg)
        verified = bool(state['model_receipt'] and state['model_receipt'] == model_fingerprint(cfg.llm))
        return {'version': 1, 'step': state['step'], 'completed': state['completed'],
                'dismissed': state['dismissed'], 'shouldOpen': not state['completed'] and not state['dismissed'],
                'keyConfigured': bool(cfg.llm.primary_key), 'modelVerified': verified,
                'wechatEnabled': bool(cfg.adapter.wechat_enabled), 'qqEnabled': bool(cfg.adapter.qq_enabled),
                'wechatLoggedIn': bool(wechat_logged_in),
                'defaultBase': DEEPSEEK_BASE, 'defaultModel': DEEPSEEK_MODEL}


async def test_primary_model(cfg) -> dict:
    """Only explicit user action calls the saved provider; never try fallback keys."""
    llm = cfg.llm
    if not isinstance(llm.primary_key,str) or not llm.primary_key or '•' in llm.primary_key:
        return {'ok': False, 'code': 'missing_key', 'message': '请先粘贴并保存 API Key'}
    if not isinstance(llm.primary_base,str) or not isinstance(llm.primary_model,str):
        return {'ok': False, 'code': 'invalid_config', 'message': '请检查模型名称与接口地址'}
    try:
        parsed = urlsplit(llm.primary_base)
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        valid = False
    if not valid or not llm.primary_model:
        return {'ok': False, 'code': 'invalid_config', 'message': '请检查模型名称与接口地址'}
    base = llm.primary_base.rstrip('/')
    body = {'model': llm.primary_model, 'messages': [{'role': 'user', 'content': '只回复OK'}],
            'max_tokens': 32, 'stream': False}
    if parsed.hostname == 'api.deepseek.com':
        body['thinking'] = {'type': 'disabled'}
    try:
        async with make_client(base, timeout=15) as client:
            # Refuse redirects instead of sending the saved key to an unexpected host.
            response = await client.post(base + '/chat/completions', json=body,
                headers={'Authorization': 'Bearer ' + llm.primary_key}, follow_redirects=False)
        status = response.status_code
        errors = {401: ('invalid_key', 'API Key 验证失败，请重新复制完整 Key'),
                  403: ('forbidden', '平台暂未允许该 Key 调用模型，请检查账户权限'),
                  402: ('balance', 'API 账户余额不足，请在开放平台检查余额'),
                  404: ('model', '接口或模型未找到，请检查模型名称与地址'),
                  429: ('rate_limit', '调用过于频繁或配额已用完，请稍后重试')}
        if status in errors:
            code, message = errors[status]
            return {'ok': False, 'code': code, 'message': message}
        if status != 200:
            return {'ok': False, 'code': 'provider', 'message': f'模型接口返回 HTTP {status}，请稍后重试'}
        data = response.json()
        choices = data.get('choices') if isinstance(data, dict) else None
        content = choices[0].get('message', {}).get('content') if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if not isinstance(content, str) or not content.strip():
            return {'ok': False, 'code': 'empty_reply', 'message': '接口已响应，但没有返回文本，请检查模型是否支持对话'}
        return {'ok': True, 'code': 'verified', 'message': '模型连接测试通过', 'model': llm.primary_model}
    except httpx.TimeoutException:
        return {'ok': False, 'code': 'timeout', 'message': '模型接口响应超时，请检查网络后重试'}
    except Exception:
        # Do not return provider bodies/errors that may contain an API key.
        return {'ok': False, 'code': 'connection', 'message': '模型连接测试未完成，请检查网络与接口地址'}
