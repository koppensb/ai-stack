#!/usr/bin/env python3
"""Private SearXNG scheduler. One process/replica owns all provider budgets."""
import copy
import fcntl
import json
import logging
import math
import os
import random
import re
import threading
import time
import tomllib
from collections import Counter, OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

LOG = logging.getLogger('search-gateway')
POOLS = {
    'general': ('google', 'brave', 'duckduckgo'),
    'news': ('google news', 'brave.news', 'duckduckgo news'),
    'it': ('github', 'stackoverflow'),
    'science': ('arxiv',),
}
FAMILIES = {e: family for family, engines in {
    'google': ('google', 'google news'), 'brave': ('brave', 'brave.news'),
    'duckduckgo': ('duckduckgo', 'duckduckgo news'), 'wikipedia': ('wikipedia',),
    'github': ('github',), 'stackoverflow': ('stackoverflow',), 'arxiv': ('arxiv',),
}.items() for e in engines}
ALIASES = {'go': 'google', 'gon': 'google news', 'br': 'brave',
           'brnews': 'brave.news', 'ddg': 'duckduckgo', 'ddn': 'duckduckgo news',
           'wp': 'wikipedia', 'gh': 'github', 'st': 'stackoverflow', 'arx': 'arxiv',
           'web': 'general'}


class Failure(Exception):
    def __init__(self, status, message, retry_after=0):
        super().__init__(message)
        self.status, self.retry_after = status, max(0, math.ceil(retry_after))


@dataclass(frozen=True)
class Config:
    global_gap: float = 4
    jitter: float = 2
    provider_gap: float = 20
    cooldown: float = 900
    transient_cooldown: float = 60
    max_cooldown: float = 21600
    timeout: float = 12
    request_budget: float = 40
    attempts: int = 2
    queue_size: int = 8
    cache_size: int = 128
    cache_ttl: float = 300
    news_cache_ttl: float = 120
    empty_cache_ttl: float = 30

    @classmethod
    def read(cls, path):
        with open(path, 'rb') as f:
            c = cls(**tomllib.load(f))
        for name, value in vars(c).items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'Invalid numeric setting: {name}')
            if value < 0 or (name != 'jitter' and value == 0):
                raise ValueError(f'Setting must be positive: {name}')
        for name in ('attempts', 'queue_size', 'cache_size'):
            if type(getattr(c, name)) is not int:
                raise ValueError(f'{name} must be an integer')
        if c.attempts > 3 or c.provider_gap < c.global_gap or c.max_cooldown < c.cooldown:
            raise ValueError('Invalid attempt/gap/cooldown limits')
        return c


def normalize(params):
    allowed = {'q', 'format', 'engines', 'categories', 'language', 'time_range',
               'safesearch', 'pageno', 'theme', 'image_proxy'}
    if set(params) - allowed or any(len(v) != 1 for v in params.values()):
        raise Failure(400, 'Unknown or repeated search parameter')
    p = {k: v[0] for k, v in params.items()}
    if p.get('format', 'json') != 'json':
        raise Failure(400, 'Only JSON output is supported')
    # Open WebUI sends these UI options even for JSON; accept inert defaults only.
    if p.get('theme', 'simple') != 'simple' or p.get('image_proxy', '0').lower() not in ('0', 'false'):
        raise Failure(400, 'Only theme=simple and image_proxy=0 are supported')
    q = p.get('q', '').strip()
    if not q or len(q) > 2048:
        raise Failure(400, 'q must contain 1..2048 characters')
    tokens, routes = [], []
    query_tokens = q.split()
    inline_language = None
    if query_tokens[0].startswith(':'):
        inline_language = query_tokens.pop(0)[1:]
        if not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z]{2,4})?', inline_language):
            raise Failure(400, 'Invalid leading language marker')
    for token in query_tokens:
        if token.startswith('!'):
            route = token[1:].lower().replace('_', ' ').replace('-', ' ')
            route = ALIASES.get(route, route)
            if route not in POOLS and route not in FAMILIES:
                raise Failure(400, 'Unsupported search shortcut')
            routes.append(route)
        elif token.startswith(':') or re.fullmatch(r'<\d+', token):
            raise Failure(400, 'Use one leading language marker; timeout controls are unsupported')
        else:
            tokens.append(token)
    # Remove selection syntax before SearXNG sees q, so it cannot fan out again.
    q = ' '.join(tokens)
    if not q:
        raise Failure(400, 'Missing search text')
    category = p.get('categories', 'general') or 'general'
    category = ALIASES.get(category, category)
    if category not in POOLS:
        raise Failure(400, 'Select one category: general, news, it or science')
    explicit = p.get('engines', '')
    if explicit:
        choices = tuple(dict.fromkeys(e.strip() for e in explicit.split(',')))
        if any(e not in FAMILIES for e in choices):
            raise Failure(400, 'Unknown engine')
    else:
        choices = POOLS[category]
    if routes:
        if len(set(routes)) != 1 or explicit or p.get('categories'):
            raise Failure(400, 'Use one shortcut OR engines/categories parameters')
        route = routes[0]
        if route in POOLS:
            category, choices = route, POOLS[route]
        else:
            choices = (route,)
    if explicit and p.get('categories') and any(e not in POOLS[category] for e in choices):
        raise Failure(400, 'Engine does not belong to selected category')
    # Existing Open WebUI settings may still explicitly send "all". Use the
    # stack's German default for unspecified/all, preserving explicit languages.
    language = inline_language or p.get('language', '').strip()
    if language in ('', 'all'):
        language = 'de'
    if not re.fullmatch(r'(?:all|auto|[a-z]{2,3}(?:-[A-Za-z]{2,4})?)', language):
        raise Failure(400, 'Invalid language')
    if p.get('time_range', '') not in ('', 'day', 'week', 'month', 'year'):
        raise Failure(400, 'Invalid time_range')
    if p.get('safesearch', '1') not in ('0', '1', '2'):
        raise Failure(400, 'Invalid safesearch')
    page = p.get('pageno', '1')
    if not page.isdigit() or not 1 <= int(page) <= 10:
        raise Failure(400, 'pageno must be between 1 and 10')
    if int(page) > 1 and len(choices) != 1:
        raise Failure(400, 'Pagination requires one explicit engine to keep pages consistent')
    upstream = {'q': q, 'format': 'json', 'language': language,
                'safesearch': p.get('safesearch', '1'), 'pageno': str(int(page)),
                'time_range': p.get('time_range', '')}
    # Cache keys preserve case, phrase punctuation and every supported filter.
    key = json.dumps([upstream, choices], sort_keys=True)
    news = all(e in POOLS['news'] for e in choices)
    return upstream, choices, key, news


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(value):
    try:
        number = float(value)
        return max(0, number) if math.isfinite(number) else 0
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0


class Backend:
    def __init__(self, base_url):
        url = urlsplit(base_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.query or url.fragment:
            raise ValueError('Invalid SEARXNG_URL')
        self.url = base_url.rstrip('/') + '/search'
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def __call__(self, params, timeout):
        req = Request(self.url + '?' + urlencode(params), headers={
            'Accept': 'application/json', 'User-Agent': 'ai-stack-search-gateway/1.0'})
        try:
            with self.http.open(req, timeout=timeout) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Response too large')
            result = json.loads(data)
            if not isinstance(result, dict) or not isinstance(result.get('results'), list):
                raise ValueError('Invalid results')
            if not all(isinstance(r, dict) for r in result['results']):
                raise ValueError('Invalid result entry')
            errors = result.get('unresponsive_engines', [])
            if not isinstance(errors, list) or not all(
                isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str) and isinstance(e[1], str)
                for e in errors
            ):
                raise ValueError('Invalid engine errors')
            return result
        except HTTPError as e:
            # This is a backend-wide HTTP failure, not an engine failure hidden in JSON.
            pause = max(30, retry_seconds(e.headers.get('Retry-After')))
            e.close()
            raise Failure(503, 'SearXNG HTTP error', pause) from e
        except (URLError, OSError, ValueError) as e:
            raise Failure(502, 'SearXNG unavailable or invalid response', 30) from e


class Gateway:
    def __init__(self, cfg, backend, state_path, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.cfg, self.backend, self.path = cfg, backend, Path(state_path)
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(cfg.queue_size)
        self.cache = OrderedDict()
        self.stats = Counter()
        self.next_global = 0
        self.providers = {f: {'next': 0, 'blocked': 0, 'failures': 0} for f in set(FAMILIES.values())}
        self.turns = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        state = json.loads(self.path.read_text())
        # Fail closed on corrupt state instead of silently discarding active cooldowns.
        for family, values in state['providers'].items():
            if family in self.providers:
                if set(values) != {'next', 'blocked', 'failures'} or any(
                    type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values.values()
                ) or type(values['failures']) is not int:
                    raise ValueError('Invalid provider state')
                self.providers[family] = values
        self.next_global = float(state['next_global'])
        if not math.isfinite(self.next_global) or self.next_global < 0:
            raise ValueError('Invalid global state')

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as f:
            json.dump({'providers': self.providers, 'next_global': self.next_global}, f)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(self.path)

    def count(self, name):
        with self.cache_lock:
            self.stats[name] += 1

    def cached(self, key):
        with self.cache_lock:
            found = self.cache.get(key)
            if found and found[0] > self.monotonic():
                self.cache.move_to_end(key)
                self.stats['cache_hits'] += 1
                answer = copy.deepcopy(found[1])
                answer['gateway']['cache'] = True
                return answer
            self.cache.pop(key, None)

    def put(self, key, response, ttl):
        with self.cache_lock:
            self.cache[key] = (self.monotonic() + ttl, copy.deepcopy(response))
            self.cache.move_to_end(key)
            while len(self.cache) > self.cfg.cache_size:
                self.cache.popitem(last=False)

    def search(self, params):
        upstream, choices, key, news = normalize(params)
        self.count('requests')
        hit = self.cached(key)
        if hit is not None:
            return hit
        if not self.slots.acquire(blocking=False):
            raise Failure(429, 'Search queue full; retry later', self.cfg.global_gap)
        deadline = self.monotonic() + self.cfg.request_budget
        acquired = False
        try:
            acquired = self.lock.acquire(timeout=max(0, deadline - self.monotonic()))
            if not acquired:
                raise Failure(503, 'Search queue timeout', self.cfg.global_gap)
            # A queued identical query reuses the first request's cache entry.
            hit = self.cached(key)
            if hit is not None:
                return hit
            return self._search(upstream, choices, key, news, deadline)
        finally:
            if acquired:
                self.lock.release()
            self.slots.release()

    def _search(self, upstream, choices, key, news, deadline):
        attempted, errors = [], []
        had_clean_empty = False
        for _ in range(min(self.cfg.attempts, len(choices))):
            now = self.clock()
            start = self.turns.get(choices, 0) % len(choices)
            ordered = choices[start:] + choices[:start]
            eligible = [e for e in ordered if e not in attempted and
                        self.providers[FAMILIES[e]]['blocked'] <= now]
            if not eligible:
                break
            ready = lambda e: max(self.next_global, self.providers[FAMILIES[e]]['next'])
            # Rotate ready providers; otherwise wait for the earliest eligible one.
            engine = next((e for e in eligible if ready(e) <= now), min(eligible, key=ready))
            wait = max(0, ready(engine) - now)
            if wait + self.cfg.timeout > deadline - self.monotonic():
                break
            if wait:
                self.sleep(wait)
            family = FAMILIES[engine]
            provider = self.providers[family]
            self.turns[choices] = choices.index(engine) + 1
            attempted.append(engine)
            # Persist reservations before I/O so restart cannot instantly re-query.
            self.next_global = self.clock() + self.cfg.global_gap + random.uniform(0, self.cfg.jitter)
            provider['next'] = self.clock() + self.cfg.provider_gap
            self._save()
            self.count('upstream_calls')
            try:
                # SearXNG UNIONs engines and categories. Forwarding a category
                # here would re-enable every engine in it and defeat scheduling.
                # normalize() already resolved the category into eligible engines.
                response = self.backend(dict(upstream, engines=engine), self.cfg.timeout)
            except Failure as e:
                self.next_global = max(self.next_global, self.clock() + e.retry_after)
                self._save()
                self.count('backend_errors')
                raise
            # Gaps apply after completion too, preventing a burst after a slow call.
            self.next_global = max(self.next_global, self.clock() + self.cfg.global_gap + random.uniform(0, self.cfg.jitter))
            provider['next'] = max(provider['next'], self.clock() + self.cfg.provider_gap)
            reason = next((e[1] for e in response.get('unresponsive_engines', []) if e[0] == engine), '')
            if reason:
                provider['failures'] += 1
                blocked = any(word in reason.lower() for word in ('captcha', '429', '403', 'too many', 'denied', 'blocked'))
                base = self.cfg.cooldown if blocked else self.cfg.transient_cooldown
                delay = min(self.cfg.max_cooldown, base * 2 ** min(provider['failures'] - 1, 10))
                provider['blocked'] = self.clock() + delay
                errors.append([engine, reason])
                self.count('provider_errors')
                LOG.info('provider=%s paused_seconds=%s', family, delay)
            else:
                provider['failures'] = 0
                provider['blocked'] = 0
            self._save()
            if response['results']:
                response = copy.deepcopy(response)
                response['query'] = upstream['q']
                response['unresponsive_engines'] = errors
                response['gateway'] = {'cache': False, 'attempted_engines': attempted[:], 'selected_engine': engine}
                self.put(key, response, self.cfg.news_cache_ttl if news else self.cfg.cache_ttl)
                return response
            if not reason:
                had_clean_empty = True
        if had_clean_empty:
            response = {'query': upstream['q'], 'results': [], 'unresponsive_engines': errors,
                        'gateway': {'cache': False, 'attempted_engines': attempted[:]}}
            self.put(key, response, self.cfg.empty_cache_ttl)
            return response
        now = self.clock()
        retry = min(max(self.next_global, self.providers[FAMILIES[e]]['next'],
                        self.providers[FAMILIES[e]]['blocked']) for e in choices) - now
        raise Failure(503, 'No search provider available within request budget', max(1, retry))

    def metrics(self):
        with self.cache_lock:
            values = dict(self.stats)
            size = len(self.cache)
        lines = [f'ai_search_gateway_{name}_total {values.get(name, 0)}' for name in
                 ('requests', 'cache_hits', 'upstream_calls', 'provider_errors', 'backend_errors')]
        # Fixed provider labels only; never expose queries, URLs or cache keys.
        for family, provider in sorted(self.providers.items()):
            seconds = max(0, provider['blocked'] - self.clock())
            lines.append(f'ai_search_gateway_provider_cooldown_seconds{{provider="{family}"}} {seconds:.0f}')
        return '\n'.join(lines + [f'ai_search_gateway_cache_entries {size}', ''])


def make_server(gateway, address):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Never log URLs/query text or response bodies.

        def send(self, status, data, content_type='application/json', retry=0):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                if retry:
                    self.send_header('Retry-After', str(retry))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def do_HEAD(self):
            if urlsplit(self.path).path == '/healthz':
                self.send(200, {'status': 'ok'})
            else:
                self.send(405, {'error': 'Use GET or POST'})

        def do_GET(self):
            self.handle_search()

        def do_POST(self):
            self.handle_search()

        def handle_search(self):
            path = urlsplit(self.path)
            try:
                if self.command == 'GET' and path.path == '/healthz':
                    return self.send(200, {'status': 'ok'})
                if self.command == 'GET' and path.path == '/metrics':
                    return self.send(200, gateway.metrics().encode(), 'text/plain; version=0.0.4')
                if path.path not in ('/', '/search'):
                    return self.send(404, {'error': 'Not found'})
                raw = path.query
                if self.command == 'POST':
                    if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                        raise Failure(415, 'Use application/x-www-form-urlencoded')
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                    except ValueError as e:
                        raise Failure(400, 'Invalid Content-Length') from e
                    if not 0 < length <= 8192 or path.query or self.headers.get('Transfer-Encoding'):
                        raise Failure(400, 'Invalid form request')
                    raw = self.rfile.read(length).decode('utf-8')
                if len(raw) > 8192:
                    raise Failure(413, 'Search request too large')
                params = parse_qs(raw, keep_blank_values=True, max_num_fields=16)
                return self.send(200, gateway.search(params))
            except Failure as e:
                return self.send(e.status, {'error': str(e)}, retry=e.retry_after)
            except (ValueError, UnicodeError):
                return self.send(400, {'error': 'Invalid request encoding'})
            except Exception:
                LOG.exception('Internal gateway error')
                return self.send(500, {'error': 'Internal gateway error'})

    return ThreadingHTTPServer(address, Handler)


def main():
    logging.basicConfig(level=logging.INFO)
    cfg = Config.read(os.getenv('GATEWAY_CONFIG', '/etc/search-gateway/settings.toml'))
    state = Path(os.getenv('GATEWAY_STATE', '/state/providers.json'))
    state.parent.mkdir(parents=True, exist_ok=True)
    owner = state.with_suffix('.lock').open('a')
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gateway = Gateway(cfg, Backend(os.getenv('SEARXNG_URL', 'http://searxng:8080')), state)
    # No replicas/workers: one scheduler and one persistent provider budget.
    server = make_server(gateway, ('0.0.0.0', int(os.getenv('PORT', '8080'))))
    LOG.info('Private search gateway listening on %s', server.server_address[1])
    server.serve_forever()


if __name__ == '__main__':
    main()
