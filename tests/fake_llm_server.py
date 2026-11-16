#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""本地假 LLM 服务（**只用于单元测试**）。

- 只监听 127.0.0.1，不访问任何外部网络，更不涉及 B 站接口
- 支持 OpenAI 兼容格式（SSE / 非流式）与 Gemini 原生格式
- 刻意实现了"慢块""慢响应""先 500 再成功""401""坏 JSON""半截流"等
  现场故障，用来回归验证客户端的分块超时、重试与部分流处理
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 假服务默认返回的答案文本
ANSWER_TEXT = '答案：3'
#: 分块返回时的切片
ANSWER_PIECES = ['答案', '：', '3']


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'FakeLLM/1.0'
    sys_version = ''

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):  # noqa: A003 - 保持签名
        """静音：避免测试输出被访问日志淹没"""

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length else b''
        try:
            data = json.loads(raw or b'{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _count(self, key):
        counts = self.server.state.setdefault('counts', {})
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:  # pragma: no cover - 客户端提前断开，属预期情况
            pass

    def _raw(self, status, body, content_type='text/plain; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:  # pragma: no cover - 客户端提前断开，属预期情况
            pass

    def _start_sse(self):
        """开始 SSE 响应：不设 Content-Length，用 Connection: close 表示结束"""
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
        except OSError:  # pragma: no cover - 客户端提前断开，属预期情况
            pass

    def _sse(self, payload):
        self._write(b'data: ' + json.dumps(payload, ensure_ascii=False).encode('utf-8') + b'\n\n')

    def _sse_done(self):
        self._write(b'data: [DONE]\n\n')

    def _write(self, chunk):
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except OSError:  # pragma: no cover - 客户端提前断开，属预期情况
            raise _ClientGone()

    # ---------- 响应构造 ----------

    def _openai_json(self):
        self._json(200, {
            'id': 'chatcmpl-test',
            'object': 'chat.completion',
            'model': 'fake-model',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ANSWER_TEXT},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17},
        })

    def _openai_stream(self, pieces=None, delay=0.0):
        pieces = list(ANSWER_PIECES if pieces is None else pieces)
        self._start_sse()
        self._sse({'id': 'chatcmpl-test', 'object': 'chat.completion.chunk',
                   'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]})
        for piece in pieces:
            if delay:
                time.sleep(delay)
            self._sse({'id': 'chatcmpl-test', 'object': 'chat.completion.chunk',
                       'choices': [{'index': 0, 'delta': {'content': piece}}]})
        self._sse({'id': 'chatcmpl-test', 'object': 'chat.completion.chunk',
                   'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17}})
        self._sse_done()

    def _gemini_json(self):
        self._json(200, {
            'candidates': [{'content': {'role': 'model', 'parts': [{'text': ANSWER_TEXT}]},
                            'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 5},
        })

    def _gemini_stream(self, pieces=None, delay=0.0, thought_pieces=None):
        pieces = list(ANSWER_PIECES if pieces is None else pieces)
        self._start_sse()
        for thought in thought_pieces or []:
            self._sse({'candidates': [{'content': {'role': 'model',
                                                   'parts': [{'text': thought, 'thought': True}]}}]})
        for piece in pieces:
            if delay:
                time.sleep(delay)
            self._sse({'candidates': [{'content': {'role': 'model', 'parts': [{'text': piece}]}}]})
        self._sse({'candidates': [{'content': {'role': 'model', 'parts': [{'text': ''}]},
                                   'finishReason': 'STOP'}],
                   'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 5}})

    def _openai_reasoning_stream(self, reasoning_pieces, answer_pieces=None):
        """deepseek-reasoner 风格：先吐 reasoning_content，再吐 content。"""
        self._start_sse()
        for piece in reasoning_pieces:
            self._sse({'id': 'chatcmpl-test', 'object': 'chat.completion.chunk',
                       'choices': [{'index': 0, 'delta': {'reasoning_content': piece}}]})
        for piece in answer_pieces or []:
            self._sse({'id': 'chatcmpl-test', 'object': 'chat.completion.chunk',
                       'choices': [{'index': 0, 'delta': {'content': piece}}]})
        self._sse({'id': 'chatcmpl-test', 'object': 'chat.completion.chunk',
                   'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17}})
        self._sse_done()

    # ---------- 路由 ----------

    def do_POST(self):  # noqa: N802 - http.server 约定
        try:
            self._route()
        except _ClientGone:  # pragma: no cover - 客户端提前断开
            pass

    def _route(self):
        path = self.path
        body = self._read_body()
        self.server.state['last_body'] = body
        self.server.state['last_headers'] = dict(self.headers)
        route = path.split('?')[0].strip('/').split('/')[0] if path.strip('/') else ''
        self._count(route)

        gemini_path = ':streamGenerateContent' in path or ':generateContent' in path
        gemini_stream = ':streamGenerateContent' in path

        if route == 'slowstream':
            # 4 块 × 0.2s：总耗时 0.8s，但块间隔只有 0.2s
            self._openai_stream(['答', '案', '：', '3'], delay=0.2)
        elif route == 'geminislow':
            self._gemini_stream(ANSWER_PIECES, delay=0.2)
        elif route == 'slow':
            # 迟迟不给响应头/正文，触发读超时
            time.sleep(2.0)
            self._openai_json()
        elif route == 'flaky':
            # 前 flaky_fail_times 次返回 500，之后成功
            fail_times = int(self.server.state.get('flaky_fail_times', 1))
            if self._count('flaky_total') <= fail_times:
                self._json(500, {'error': {'message': 'internal error (fake)'}})
                return
            if body.get('stream'):
                self._openai_stream()
            else:
                self._openai_json()
        elif route == 'ratelimit':
            fail_times = int(self.server.state.get('ratelimit_fail_times', 1))
            if self._count('ratelimit_total') <= fail_times:
                self._json(429, {'error': {'message': 'rate limited (fake)'}})
                return
            if body.get('stream'):
                self._openai_stream()
            else:
                self._openai_json()
        elif route == 'auth':
            self._json(401, {'error': {'message': 'invalid api key (fake)'}})
        elif route == 'broken':
            self._raw(200, 'this is definitely not json')
        elif route == 'partial':
            # 先给一块，然后长时间静默：客户端应带着已收内容返回
            self._start_sse()
            self._sse({'id': 'chatcmpl-test',
                       'choices': [{'index': 0, 'delta': {'content': '答案：3'}}]})
            time.sleep(3.0)
        elif route == 'ignorant':
            # 忽略 "stream": true，直接返回完整 JSON（部分中转站/网关的行为）
            self._openai_json()
        elif route == 'reasoning':
            # 推理模型：思维链 + 正文
            self._openai_reasoning_stream(
                ['先看选项：1. 甲 2. 乙。', '对比之下第三个最合适。'],
                ANSWER_PIECES,
            )
        elif route == 'reasoningonly':
            # 正文为空，结论只在思维链里
            self._openai_reasoning_stream(['分析后确定：', '答案：3'])
        elif route == 'gemthou':
            # Gemini 带思考摘要的分段
            self._gemini_stream(ANSWER_PIECES, thought_pieces=['思考中……答案：1？']) 
        elif route == 'gemini' or route == 'v1beta' or gemini_path:
            if gemini_stream:
                self._gemini_stream()
            else:
                self._gemini_json()
        else:
            # 默认按请求体里的 stream 字段决定返回形式
            if body.get('stream'):
                self._openai_stream()
            else:
                self._openai_json()


class _ClientGone(Exception):
    """客户端提前断开连接"""


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """静音：客户端超时后主动断开连接是预期行为，不必打印堆栈"""


class FakeLLMServer:
    """线程化的假 LLM 服务，作为 context manager 使用"""

    def __init__(self):
        self.state = {'counts': {}, 'flaky_fail_times': 1, 'ratelimit_fail_times': 1}
        self._httpd = _QuietServer(('127.0.0.1', 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self.state
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def base(self):
        return f'http://127.0.0.1:{self.port}'

    def counts(self):
        return dict(self.state.get('counts') or {})

    def count(self, route):
        return int((self.state.get('counts') or {}).get(route, 0))

    def reset_counts(self):
        self.state['counts'] = {}
