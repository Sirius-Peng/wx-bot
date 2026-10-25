"""DeepSeek 1017 Search/Fetch wire API, without an auxiliary LLM call.

Only fixed official endpoints receive the bearer key. Public page text remains
untrusted evidence, never instructions. Tool requests have request-count billing,
not model token usage; diagnostics intentionally do not manufacture token fields.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import logging
import math
import re
import time
import unicodedata
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from .http import make_client
from .reality import clock_context
from .web_search import DeepSeekWebSearch, _Flight, query_is_public

log = logging.getLogger(__name__)
SEARCH_ENDPOINT = 'https://api.deepseek.com/v1/tools/search'
FETCH_ENDPOINT = 'https://api.deepseek.com/v1/tools/fetch'
RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 3500
MAX_SUMMARY_CHARS = 1200
MAX_SNIPPET_CHARS = 500
MAX_FETCH_CHARS = 12000
PRIVATE_QUERY_KEYS = frozenset({
    'key', 'apikey', 'api_key', 'api-key', 'token', 'access_token', 'refresh_token',
    'id_token', 'authorization', 'auth', 'password', 'passwd', 'pwd', 'secret',
    'client_secret', 'signature', 'sig', 'credential', 'credentials', 'session',
    'sessionid', 'session_id', 'session_token', 'sessiontoken', 'auth_token', 'authtoken',
    'access-token', 'accesstoken', 'auth_key', 'authkey', 'authcode', 'code', 'ticket', 'jwt', 'x-amz-credential',
    'x-amz-signature', 'x-goog-credential', 'x-goog-signature',
})
_QUERY_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_SECRET_TEXT = re.compile(r'(?:sk-[a-zA-Z0-9_-]{8,}|Bearer\s+\S+|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)', re.I)


def public_fetch_url(value):
    """Conservative project URL boundary; return the original URL, not a rewrite."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ''
    if value != value.strip() or any(unicodedata.category(c) in ('Cc', 'Cs') for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        # A malformed port is not a valid public URL. Arbitrary public ports are
        # not fetched locally; the official Fetch service performs the request.
        parsed.port
        host = parsed.hostname.rstrip('.').lower()
        if '%' in host or '\\' in parsed.netloc or host == 'localhost' or host.endswith(('.local', '.internal', '.localhost', '.lan', '.home')):
            return ''
        try:
            if not ipaddress.ip_address(host).is_global:
                return ''
        except ValueError:
            if '.' not in host or any(c.isspace() for c in host) or all(re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)', part) for part in host.split('.')):
                return ''
        # Signed links and login/reset links carry credentials even though their
        # hostname is public. Never send these to a third-party fetch endpoint.
        if any(name.lower() in PRIVATE_QUERY_KEYS for name, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            return ''
        decoded = unquote(value)
        if _SECRET_TEXT.search(decoded):
            return ''
        if parsed.fragment and re.search(r'(?:token|secret|password|credential|session|auth)\s*[:=]', unquote(parsed.fragment), re.I):
            return ''
        return value
    except (ValueError, UnicodeError):
        return ''


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _score(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 3:
        return value
    return None


def parse_search(payload):
    """Keep model summaries separate from fetched content, with bounded evidence."""
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        return {'ok': False, 'error': 'invalid_response', 'sources': [], 'summary': '', 'body_available': False}
    candidates = []
    for index, item in enumerate(payload['results'][:50]):
        if not isinstance(item, dict):
            continue
        url = public_fetch_url(item.get('url'))
        if not url:
            continue
        content = _text(item.get('content'), MAX_CONTENT_CHARS)
        summary = _text(item.get('summary'), MAX_SUMMARY_CHARS)
        snippet = _text(item.get('snippet'), MAX_SNIPPET_CHARS)
        authority = item.get('authority')
        if not isinstance(authority, int) or isinstance(authority, bool) or not -1 <= authority <= 4:
            authority = None
        date = _text(item.get('date'), 100)
        source = {
            'url': url, 'title': _text(item.get('title'), 200) or url,
            'content': content, 'summary': summary, 'snippet': snippet,
            'date': date, 'published_at': date,
            'site_name': _text(item.get('site_name'), 100),
            'authority': authority, 'rerank_score': _score(item.get('rerank_score')),
            'evidence_kind': 'content' if content else 'summary' if summary else 'snippet' if snippet else 'title_only',
        }
        for field, limit in (('content', MAX_CONTENT_CHARS), ('summary', MAX_SUMMARY_CHARS), ('snippet', MAX_SNIPPET_CHARS)):
            if isinstance(item.get(field), str) and len(item[field].strip()) > limit:
                source[field + '_truncated'] = True
        # The API already ranks results; valid scores allow a malformed or
        # unsorted payload to be normalized without inventing a quality score.
        candidates.append((source, index))
    candidates.sort(key=lambda pair: (-(pair[0]['rerank_score'] if pair[0]['rerank_score'] is not None else -1), pair[1]))
    sources = []
    seen = set()
    for source, _ in candidates:
        if source['url'] in seen:
            continue
        seen.add(source['url']); sources.append(source)
        if len(sources) == 3:
            break
    return {
        'ok': bool(sources), 'error': '' if sources else 'no_results', 'sources': sources, 'summary': '',
        'body_available': any(source['content'] or source['summary'] for source in sources),
        'evidence_available': any(source['content'] or source['summary'] or source['snippet'] for source in sources),
        'full_page_available': any(source['content'] for source in sources) and not any(source.get('content_truncated') for source in sources),
    }


def parse_fetch(payload, url):
    if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
        return {'ok': False, 'error': 'invalid_response', 'sources': [], 'summary': '', 'body_available': False}
    raw = payload['content'].strip()
    if not raw:
        return {'ok': False, 'error': 'no_content', 'sources': [], 'summary': '', 'body_available': False}
    source = {
        'url': url, 'title': _text(payload.get('title'), 200) or url,
        'content': raw[:MAX_FETCH_CHARS], 'summary': '', 'snippet': '',
        'date': '', 'published_at': '', 'site_name': '', 'authority': None,
        'rerank_score': None, 'evidence_kind': 'content',
    }
    if len(raw) > MAX_FETCH_CHARS:
        source['content_truncated'] = True
    return {'ok': True, 'error': '', 'sources': [source], 'summary': '',
            'body_available': True, 'evidence_available': True, 'full_page_available': len(raw) <= MAX_FETCH_CHARS}


class DeepSeekSearchFetch(DeepSeekWebSearch):
    """Reuse bounded result cache and single-flight lifecycle, not model requests."""
    supports_fetch = True
    backend = 'search_fetch'

    def __init__(self, key='', model='deepseek-flash', context_timezone='Asia/Shanghai'):
        super().__init__(key, model, context_timezone)
        self.tool_attempts = self.attempts
        self._sleep = asyncio.sleep

    async def search(self, query):
        if not isinstance(query, str) or len(query) > 128 or not query_is_public(query):
            return {'ok': False, 'error': 'invalid_or_private_query', 'sources': []}
        if any(unicodedata.category(c) == 'Cs' or c == '\x00' for c in query):
            return {'ok': False, 'error': 'invalid_or_private_query', 'sources': []}
        # A public-looking query can still include a signed/reset URL. Reject
        # it rather than forwarding or silently rewriting the private query.
        if any(not public_fetch_url(match.group(0).rstrip('.,!?，。！？;；)]}')) for match in _QUERY_URL.finditer(query)):
            return {'ok': False, 'error': 'invalid_or_private_query', 'sources': []}
        # Only trim the boundary: quoted double spaces and query operators have
        # semantics. Do not collapse whitespace or silently truncate a query.
        return await self._run('search', query.strip())

    async def fetch(self, url):
        if not public_fetch_url(url):
            return {'ok': False, 'error': 'invalid_or_private_url', 'sources': []}
        return await self._run('fetch', url)

    async def _run(self, operation, value):
        empty = {'ok': False, 'sources': []}
        if not self.key:
            return {**empty, 'error': 'missing_deepseek_key'}
        if self._closed:
            return {**empty, 'error': 'search_closed'}
        cache_key = (operation, *self._cache_key(value))
        cached = self._cached(cache_key)
        if cached is not None:
            return self._reused(cached, 'ttl')
        flight = self._flights.get(cache_key)
        if flight is not None and flight.task.done():
            self._flights.pop(cache_key, None); flight = None
        joined = flight is not None
        if flight is None:
            task = asyncio.create_task(self._perform_tool(operation, value, cache_key, self.key, self.context_timezone))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard); task.add_done_callback(self._consume)
            flight = _Flight(task); self._flights[cache_key] = flight
        flight.waiters += 1
        try:
            result = await asyncio.shield(flight.task)
            # Joining a failed request is not a successful cached search.
            if joined and result.get('ok'):
                return self._reused(result, 'single_flight')
            return copy.deepcopy(result)
        finally:
            flight.waiters -= 1
            if not flight.waiters:
                if self._flights.get(cache_key) is flight:
                    self._flights.pop(cache_key, None)
                if not flight.task.done():
                    flight.task.cancel()

    async def _perform_tool(self, operation, value, cache_key, key, context_timezone):
        try:
            result = await asyncio.wait_for(self._with_retry(operation, value, key), timeout=20)
        except (httpx.HTTPError, TimeoutError, asyncio.TimeoutError):
            return {'ok': False, 'error': operation + '_network_error', 'sources': []}
        except (ValueError, TypeError, KeyError, UnicodeError):
            return {'ok': False, 'error': 'invalid_response', 'sources': []}
        result.update(local_result_reuse=False, reuse_source='provider', operation=operation,
                      retrieved_at=clock_context(context_timezone))
        if not self._closed:
            self._remember(cache_key, result)
        return result

    @staticmethod
    def _retry_delay(value):
        try:
            delay = float(value)
            if not math.isfinite(delay):
                return 0.25
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return 0.25
        return max(0, min(delay, 1.0))

    async def _with_retry(self, operation, value, key):
        for attempt in range(2):
            result, status, retry_after = await self._request_tool(operation, value, key)
            if attempt == 0 and (status in (408, 429) or status is not None and 500 <= status <= 599):
                await self._sleep(self._retry_delay(retry_after))
                continue
            return result

    async def _request_tool(self, operation, value, key):
        endpoint = SEARCH_ENDPOINT if operation == 'search' else FETCH_ENDPOINT
        body = {'query': value, 'count': 5, 'need_content': True} if operation == 'search' else {'url': value}
        started = time.monotonic()
        event = {'kind': 'tool_request', 'operation': operation, 'request_id': uuid.uuid4().hex,
                 'task': 'search', 'status': 'inflight', 'started_at': time.time(),
                 'http_status': None, 'result_count': None, 'billable_result': None}
        self.tool_attempts.append(event)
        empty = {'ok': False, 'sources': []}
        try:
            if self._client is None:
                self._client = make_client(SEARCH_ENDPOINT, timeout=18)
            async with self._client.stream('POST', endpoint, json=body, follow_redirects=False,
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                timeout=httpx.Timeout(18, connect=4, pool=1)) as response:
                event['http_status'] = response.status_code
                if response.status_code != 200:
                    event.update(status='error', billable_result=False)
                    return ({**empty, 'error': operation + '_http_' + str(response.status_code)},
                            response.status_code, response.headers.get('Retry-After'))
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > RESPONSE_BYTES:
                        event['status'] = 'error'
                        return {**empty, 'error': 'response_too_large'}, 200, None
                    data.extend(chunk)
                payload = json.loads(data)
                if operation == 'search':
                    entries = payload.get('results') if isinstance(payload, dict) else None
                    count = len(entries) if isinstance(entries, list) else None
                    event.update(result_count=count, billable_result=bool(count) if count is not None else None)
                    result = parse_search(payload)
                else:
                    content = payload.get('content') if isinstance(payload, dict) else None
                    count = int(bool(content.strip())) if isinstance(content, str) else None
                    event.update(result_count=count, billable_result=bool(count) if count is not None else None)
                    result = parse_fetch(payload, value)
                event['status'] = 'success' if result.get('ok') else 'empty' if result.get('error') in ('no_results', 'no_content') else 'error'
                return result, 200, None
        except asyncio.CancelledError:
            event['status'] = 'cancelled'
            raise
        except Exception as error:
            event.update(status='error', error_type=type(error).__name__)
            raise
        finally:
            event.update(elapsed_ms=round((time.monotonic() - started) * 1000, 2), finished_at=time.time())
            # No URL, query, API key, response body, or fictitious zero-token usage.
            log.info('WEB_TOOL_REQUEST %s', json.dumps(event, ensure_ascii=False, separators=(',', ':')))
            self._observe(event)
