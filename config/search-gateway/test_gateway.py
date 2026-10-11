import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from gateway import Backend, Config, Failure, Gateway, make_server, normalize


class Clock:
    def __init__(self): self.now = 1000
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


def params(q='test', **kwargs):
    return {k: [v] for k, v in dict(q=q, **kwargs).items()}


def answer(engine, results=True, error=None):
    return {'results': [{'url': 'https://example.org', 'title': 'Example', 'content': 'Text', 'engine': engine}] if results else [],
            'unresponsive_engines': [[engine, error, False]] if error else []}


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.clock = Clock()
        self.calls = []
        self.reply = lambda engine: answer(engine)
        def backend(p, timeout):
            self.calls.append((self.clock(), dict(p)))
            return self.reply(p['engines'])
        self.backend = backend
        self.cfg = replace(Config(), jitter=0)
        self.g = Gateway(self.cfg, backend, self.path, self.clock, self.clock, self.clock.sleep)

    def test_round_robin_spacing_and_filters(self):
        for i in range(4):
            self.g.search(params(str(i), language='de-AT', time_range='day', safesearch='2'))
        self.assertEqual([p['engines'] for _, p in self.calls], ['google', 'brave', 'duckduckgo', 'google'])
        self.assertEqual([t for t, _ in self.calls], [1000, 1004, 1008, 1020])
        self.assertTrue(all(p['language']=='de-AT' and p['time_range']=='day' and p['safesearch']=='2' for _, p in self.calls))

    def test_captcha_fallback_shared_news_cooldown_persists(self):
        self.reply = lambda e: answer(e, False, 'CAPTCHA') if e == 'google' else answer(e)
        r = self.g.search(params())
        self.assertEqual(r['gateway']['attempted_engines'], ['google', 'brave'])
        self.assertEqual(self.g.providers['google']['blocked'], 1900)
        g = Gateway(self.cfg, self.backend, self.path, self.clock, self.clock, self.clock.sleep)
        r = g.search(params('headlines !news'))
        self.assertNotIn('google news', r['gateway']['attempted_engines'])
        self.assertNotIn('categories', self.calls[-1][1])
        self.assertEqual(self.calls[-1][1]['q'], 'headlines')

    def test_repeated_blocks_back_off(self):
        self.reply = lambda e: answer(e, False, 'Too many requests')
        for expected in (900, 1800):
            now = self.clock()
            with self.assertRaises(Failure): self.g.search(params(engines='google'))
            self.assertEqual(self.g.providers['google']['blocked'] - now, expected)
            self.clock.sleep(expected)

    def test_cache_filters_expiry_and_news_ttl(self):
        self.g.search(params())
        self.assertTrue(self.g.search(params())['gateway']['cache'])
        self.g.search(params(language='en'))
        self.assertEqual(len(self.calls), 2)
        self.clock.sleep(301)
        self.g.search(params())
        self.assertEqual(len(self.calls), 3)
        self.g.search(params('test !news'))
        self.clock.sleep(121)
        self.g.search(params('test !news'))
        self.assertEqual(len(self.calls), 5)

    def test_empty_fallback_and_short_cache(self):
        self.reply = lambda e: answer(e, False)
        self.assertEqual(self.g.search(params())['results'], [])
        self.assertEqual(len(self.calls), 2)
        self.g.search(params())
        self.assertEqual(len(self.calls), 2)
        self.clock.sleep(31)
        self.g.search(params())
        self.assertEqual(len(self.calls), 4)

    def test_all_blocked_no_calls_and_retry_after(self):
        for p in self.g.providers.values(): p['blocked'] = 1900
        with self.assertRaises(Failure) as cm: self.g.search(params())
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(cm.exception.retry_after, 900)
        self.assertEqual(self.calls, [])

    def test_backend_failure_does_not_fan_out(self):
        def fail(*_): raise Failure(503, 'down', 120)
        self.g.backend = fail
        with self.assertRaises(Failure): self.g.search(params())
        self.assertEqual(self.g.stats['upstream_calls'], 1)
        self.assertEqual(self.g.next_global, 1120)

    def test_budget_does_not_trigger_an_extra_call(self):
        self.g.next_global = self.clock() + 100
        with self.assertRaises(Failure): self.g.search(params())
        self.assertEqual(self.calls, [])

    def test_explicit_engine_page_and_lru(self):
        self.g.cfg = replace(self.cfg, cache_size=1)
        self.g.search(params('one', engines='wikipedia', pageno='2'))
        self.assertEqual(self.calls[-1][1]['pageno'], '2')
        self.g.search(params('two'))
        self.assertEqual(len(self.g.cache), 1)

    def test_corrupt_state_is_not_silently_discarded(self):
        self.path.write_text('{broken')
        with self.assertRaises(ValueError): Gateway(self.cfg, self.backend, self.path)

    def test_concurrent_identical_queries_coalesce(self):
        calls = []
        def backend(p, timeout):
            calls.append(p)
            time.sleep(.05)
            return answer(p['engines'])
        g = Gateway(self.cfg, backend, self.path)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: g.search(params()), range(6)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(r['gateway']['cache'] for r in results), 5)

    def test_queue_is_bounded(self):
        g = Gateway(replace(self.cfg, queue_size=1), self.backend, self.path)
        g.slots.acquire()
        with self.assertRaises(Failure) as cm: g.search(params())
        self.assertEqual(cm.exception.status, 429)
        g.slots.release()

    def test_different_users_share_one_scheduler(self):
        intervals = []
        def backend(p, timeout):
            start = time.monotonic()
            time.sleep(.015)
            intervals.append((start, time.monotonic()))
            return answer(p['engines'])
        cfg = replace(self.cfg, global_gap=.025, provider_gap=.1, timeout=.1, request_budget=3)
        g = Gateway(cfg, backend, self.path)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda i: g.search(params(str(i))), range(3)))
        self.assertEqual(len(intervals), 3)
        for previous, current in zip(intervals, intervals[1:]):
            self.assertGreaterEqual(current[0] - previous[1], .024)

    def test_backend_down_does_not_poison_provider_health(self):
        def backend(*_): raise Failure(502, 'down', 30)
        self.g.backend = backend
        with self.assertRaises(Failure): self.g.search(params())
        self.assertTrue(all(p['failures'] == 0 for p in self.g.providers.values()))

    def test_provider_spacing_survives_restart(self):
        self.g.search(params(engines='google'))
        g = Gateway(self.cfg, self.backend, self.path, self.clock, self.clock, self.clock.sleep)
        g.search(params('other', engines='google news'))
        self.assertEqual(self.calls[-1][0], 1020)

    def test_metrics_do_not_contain_queries(self):
        self.g.search(params('private query'))
        text = self.g.metrics()
        self.assertIn('ai_search_gateway_upstream_calls_total 1', text)
        self.assertNotIn('private query', text)


class InputValidation(unittest.TestCase):
    def test_reject_routing_bypass_and_unknown_options(self):
        for p in [params('test !!google'), params('test !images'), params('!news test !google'),
                  params('test', engines='google,unknown'), params('test', categories='general,news'),
                  params('test', format='html'), params('test', proxy='anything'),
                  params('test', pageno='2'), params('test <100000'), params('test :de'),
                  {'q': ['one', 'two']}, params('!news')]:
            with self.subTest(p=p), self.assertRaises(Failure): normalize(p)

    def test_known_shortcuts_and_parameters(self):
        for bang, engine in [('!gh', 'github'), ('!arx', 'arxiv'), ('!google_news', 'google news')]:
            p, engines, _, _ = normalize(params('hello '+bang))
            self.assertEqual(engines, (engine,))
            self.assertEqual(p['q'], 'hello')
        _, choices, _, news = normalize(params(categories='news'))
        self.assertTrue(news)
        self.assertEqual(len(choices), 3)
        self.assertNotEqual(normalize(params(language='de'))[2], normalize(params(language='en'))[2])

    def test_config_file(self):
        self.assertEqual(Config.read(Path(__file__).with_name('settings.toml')).provider_gap, 20)

    def test_openwebui_default_parameters_with_news_shortcut(self):
        p, choices, _, news = normalize(params('headlines !news', format='json',
            pageno='1', safesearch='1', language='all', time_range='', categories='',
            theme='simple', image_proxy='0'))
        self.assertTrue(news)
        self.assertEqual(p['language'], 'de')
        self.assertEqual(p['q'], 'headlines')
        self.assertEqual(len(choices), 3)

    def test_german_default_and_explicit_languages(self):
        default_key = normalize(params())[2]
        for values in ({}, {'language': ''}, {'language': 'all'}, {'language': 'de'}):
            p, _, key, _ = normalize(params(**values))
            self.assertEqual(p['language'], 'de')
            self.assertEqual(key, default_key)
        for language in ('en', 'de-AT', 'auto'):
            p, _, key, _ = normalize(params(language=language))
            self.assertEqual(p['language'], language)
            self.assertNotEqual(key, default_key)


class HTTPIntegration(unittest.TestCase):
    def test_real_http_form_json_cache_and_backend_retry(self):
        calls = []
        class FakeSearx(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                p = parse_qs(urlsplit(self.path).query)
                calls.append(p)
                if p['q'] == ['fail']:
                    self.send_response(429)
                    self.send_header('Retry-After', '120')
                    self.end_headers()
                    return
                body = json.dumps(answer(p['engines'][0])).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        sx = ThreadingHTTPServer(('127.0.0.1', 0), FakeSearx)
        threading.Thread(target=sx.serve_forever, daemon=True).start()
        self.addCleanup(sx.server_close)
        self.addCleanup(sx.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            g = Gateway(replace(Config(), jitter=0), Backend(f'http://127.0.0.1:{sx.server_port}'),
                        Path(directory)/'state.json', clock, clock, clock.sleep)
            srv = make_server(g, ('127.0.0.1', 0))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            base = f'http://127.0.0.1:{srv.server_port}'
            body = urlencode({'q': 'headlines', 'categories': 'news', 'language': 'all', 'time_range': 'day'}).encode()
            with urlopen(Request(base+'/search', data=body)) as r:
                response = json.load(r)
            self.assertEqual(response['gateway']['selected_engine'], 'google news')
            self.assertEqual(calls[0]['engines'], ['google news'])
            self.assertEqual(calls[0]['language'], ['de'])
            self.assertNotIn('categories', calls[0])
            with urlopen(base+'/search?'+body.decode()) as r:
                self.assertTrue(json.load(r)['gateway']['cache'])
            self.assertEqual(len(calls), 1)
            for route in ('/healthz', '/metrics'):
                with urlopen(base+route) as r: self.assertEqual(r.status, 200)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(HTTPError) as cm:
                urlopen(base+'/search?q=fail')
            self.assertEqual(cm.exception.code, 503)
            self.assertEqual(cm.exception.headers['Retry-After'], '120')
            cm.exception.close()


if __name__ == '__main__': unittest.main()
