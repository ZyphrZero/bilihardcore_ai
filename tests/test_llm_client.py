#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""统一 LLM 客户端的单元测试。

全部请求只发往本机 127.0.0.1 上的假服务（tests/fake_llm_server.py），
**不访问任何外部网络，更不会触碰 B 站接口**。
"""

import os
import sys
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_llm_server import FakeLLMServer  # noqa: E402
from tools.LLM.client import (LLMClient, LLMConfigError, LLMError,  # noqa: E402
                              LLMHTTPError, LLMResponseError, LLMSettings,
                              LLMTimeoutError, _looks_like_read_timeout,
                              normalize_provider, parse_choice,
                              resolve_endpoint)
from tools.logger import logger  # noqa: E402


def make_settings(base_url, **overrides):
    values = dict(
        provider='custom',
        base_url=base_url,
        model='fake-model',
        api_key='sk-test-key',
        dialect='openai',
        connect_timeout=2.0,
        read_timeout=5.0,
        stream=True,
        max_retries=0,
    )
    values.update(overrides)
    return LLMSettings(**values)


def make_client(settings):
    session = requests.Session()
    # 避免测试受系统代理影响（否则会真的往代理发请求）
    session.trust_env = False
    return LLMClient(settings, session=session)


class EndpointResolutionTests(unittest.TestCase):
    def test_openai_dialect_endpoints(self):
        cases = [
            # 用户填的 Base URL, 期望的完整请求地址
            ('https://api.deepseek.com/v1', 'https://api.deepseek.com/v1/chat/completions'),
            ('https://api.deepseek.com', 'https://api.deepseek.com/v1/chat/completions'),
            ('https://www.yyapi.cloud/', 'https://www.yyapi.cloud/v1/chat/completions'),
            ('https://api.openai.com/v1/chat/completions',
             'https://api.openai.com/v1/chat/completions'),
            # 末尾 # = 我填的就是完整地址
            ('https://x.example.com/custom/path#', 'https://x.example.com/custom/path'),
            # 阿里云百炼：只填域名时自动补兼容模式前缀
            ('https://dashscope.aliyuncs.com',
             'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'),
            ('https://dashscope.aliyuncs.com/compatible-mode/v1',
             'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'),
        ]
        for base_url, expected in cases:
            with self.subTest(base_url=base_url):
                settings = make_settings(base_url)
                self.assertEqual(resolve_endpoint(settings), expected)

    def test_gemini_dialect_endpoints(self):
        model = 'gemini-2.0-flash'
        cases = [
            ('https://generativelanguage.googleapis.com/v1beta', True,
             f'https://generativelanguage.googleapis.com/v1beta/models/{model}'
             ':streamGenerateContent?alt=sse'),
            # 只填域名（旧版会因此 404）
            ('https://generativelanguage.googleapis.com', True,
             f'https://generativelanguage.googleapis.com/v1beta/models/{model}'
             ':streamGenerateContent?alt=sse'),
            ('https://generativelanguage.googleapis.com/v1beta', False,
             f'https://generativelanguage.googleapis.com/v1beta/models/{model}'
             ':generateContent'),
            (f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', True,
             f'https://generativelanguage.googleapis.com/v1beta/models/{model}'
             ':generateContent?alt=sse'),
        ]
        for base_url, stream, expected in cases:
            with self.subTest(base_url=base_url, stream=stream):
                settings = make_settings(base_url, dialect='gemini', model=model, stream=stream)
                self.assertEqual(resolve_endpoint(settings), expected)

    def test_empty_base_url_raises(self):
        with self.assertRaises(LLMConfigError):
            resolve_endpoint(make_settings(''))


class ParseChoiceTests(unittest.TestCase):
    def test_supported_formats(self):
        cases = [
            ('3', 4, 3),
            ('答案：3', 4, 3),
            ('选项 2', 4, 2),
            ('3.', 4, 3),
            ('３', 4, 3),          # 全角数字
            ('Answer: 4', 4, 4),
            ('我觉得应该选第1个', 4, 1),
        ]
        for text, max_choice, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_choice(text, max_choice), expected)

    def test_invalid_formats(self):
        cases = [
            ('12', 4, None),        # 连写的多位数字不能被当成 1 或 2
            ('0', 4, None),
            ('5', 4, None),         # 超出选项范围
            ('无法确定', 4, None),
            ('', 4, None),
            (None, 4, None),
            ('3', 0, None),
        ]
        for text, max_choice, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_choice(text, max_choice), expected)


    def test_reasoning_style_outputs_pick_the_conclusion(self):
        """带思维链的回复不能取第一个数字（否则永远是选项 1）。"""
        cases = [
            # 先罗列选项、最后给结论
            ('1. 颜真卿  2. 孟轲  3. 田英章\n答案是 2', 4, 2),
            ('选项1是甲，选项4是丁，正确的应该是选项4', 4, 4),
            ('分析：1 和 2 都不对，3 最合适。答案：3', 4, 3),
            ('Step 1: compare options.\nStep 2: conclude.\n\n答案：4', 4, 4),
        ]
        for text, max_choice, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_choice(text, max_choice), expected)


class ProviderNormalizationTests(unittest.TestCase):
    def test_choice_values_and_names(self):
        self.assertEqual(normalize_provider('1'), 'deepseek')
        self.assertEqual(normalize_provider('2'), 'gemini')
        self.assertEqual(normalize_provider('3'), 'custom')
        self.assertEqual(normalize_provider('Gemini'), 'gemini')
        self.assertEqual(normalize_provider('custom'), 'custom')
        self.assertEqual(normalize_provider('不认识'), 'deepseek')


class ClientStreamingTests(unittest.TestCase):
    def test_stream_returns_full_text(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/v1'))
            self.assertEqual(client.ask('题目'), '答案：3')
            self.assertEqual(server.count('v1'), 1)

    def test_stream_survives_gaps_longer_than_read_timeout(self):
        """回归测试：总时长 1.0s > read_timeout 0.5s，但块间隔 0.2s < 0.5s，必须成功。

        这正是线上"提供商后台成功、本地却 ReadTimeout"的根因：
        旧实现用 stream=False，read_timeout 变成了整段生成的上限。
        """
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/slowstream', read_timeout=0.5))
            text = client.ask('题目')
            self.assertEqual(text, '答案：3')

    def test_read_timeout_with_no_data_raises(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/slow', read_timeout=0.3,
                                               stream=False))
            with self.assertRaises(LLMTimeoutError):
                client.ask('题目')

    def test_stream_stall_is_reported_as_timeout_not_conn_error(self):
        """流式连接建立后一个字节都没收到就卡住 → 必须报"读取超时"。

        回归点：requests 会把流式读超时包成 ConnectionError，旧实现（以及
        日志）会把它误报成"连不上"，排查方向完全相反。
        """
        records = []
        sink_id = logger.add(lambda message: records.append(str(message)),
                             format='{message}', level='INFO')
        try:
            with FakeLLMServer() as server:
                client = make_client(make_settings(f'{server.base}/slow', read_timeout=0.3))
                with self.assertRaises(LLMTimeoutError):
                    client.ask('题目')
        finally:
            logger.remove(sink_id)

        self.assertTrue(any('[LLM] timeout#' in line for line in records),
                        f'缺少 timeout 日志: {records}')
        self.assertFalse(any('[LLM] conn-error#' in line for line in records),
                         f'不应误报为连接失败: {records}')

    def test_read_timeout_detection_helper(self):
        read_timeout_error = type('ReadTimeoutError', (OSError,), {})('Read timed out.')
        wrapper = requests.exceptions.ConnectionError(read_timeout_error)
        self.assertTrue(_looks_like_read_timeout(wrapper))
        self.assertFalse(_looks_like_read_timeout(
            requests.exceptions.ConnectionError('Connection refused')))

    def test_partial_stream_is_returned_instead_of_raising(self):
        """上游已经开始输出、随后又卡住时，直接采用已收到的内容（避免重复计费）。"""
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/partial', read_timeout=0.5))
            self.assertEqual(client.ask('题目'), '答案：3')

    def test_stream_request_tolerates_gateway_that_ignores_stream(self):
        """回归：网关忽略 "stream": true、直接回完整 JSON 时，必须照样拿到答案。

        旧实现（以及只写 SSE 解析的实现）在这里会拿到空字符串，
        然后被当成"模型答非所问"，白白把同一道题重发几次。
        """
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/ignorant'))
            self.assertEqual(client.ask('题目'), '答案：3')

    def test_gemini_stream(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/gemini', dialect='gemini',
                                               model='gemini-2.0-flash'))
            self.assertEqual(client.ask('题目'), '答案：3')

    def test_gemini_non_stream(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/gemini', dialect='gemini',
                                               model='gemini-2.0-flash', stream=False))
            self.assertEqual(client.ask('题目'), '答案：3')


class ReasoningContentTests(unittest.TestCase):
    """思维链（reasoning_content / Gemini thought parts）不能混进正文。"""

    def test_content_stream_ignores_reasoning_chunks(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/reasoning'))
            self.assertEqual(client.ask('题目'), '答案：3')

    def test_falls_back_to_reasoning_when_content_is_empty(self):
        """正文为空时用思考内容兜底，至少还能解析出答案（而不是整题白问）。"""
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/reasoningonly'))
            text = client.ask('题目')
            self.assertIn('答案：3', text)
            self.assertEqual(parse_choice(text, 4), 3)

    def test_gemini_thought_parts_are_skipped(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/gemthou', dialect='gemini',
                                               model='gemini-2.0-flash'))
            self.assertEqual(client.ask('题目'), '答案：3')


class ClientRetryTests(unittest.TestCase):
    def test_retry_on_500(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/flaky', stream=False,
                                               max_retries=1))
            self.assertEqual(client.ask('题目'), '答案：3')
            self.assertEqual(server.count('flaky_total'), 2)

    def test_retry_on_429(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/ratelimit', stream=False,
                                               max_retries=1))
            self.assertEqual(client.ask('题目'), '答案：3')
            self.assertEqual(server.count('ratelimit_total'), 2)

    def test_auth_error_is_not_retried(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/auth', stream=False,
                                               max_retries=2))
            with self.assertRaises(LLMHTTPError) as ctx:
                client.ask('题目')
            self.assertEqual(ctx.exception.status, 401)
            self.assertEqual(server.count('auth'), 1)

    def test_gives_up_after_retries(self):
        with FakeLLMServer() as server:
            server.state['flaky_fail_times'] = 99  # 一直 500
            client = make_client(make_settings(f'{server.base}/flaky', stream=False,
                                               max_retries=1))
            with self.assertRaises(LLMHTTPError):
                client.ask('题目')
            self.assertEqual(server.count('flaky_total'), 2)


class ClientResponseTests(unittest.TestCase):
    def test_non_json_body_raises_response_error(self):
        with FakeLLMServer() as server:
            client = make_client(make_settings(f'{server.base}/broken', stream=False))
            with self.assertRaises(LLMResponseError):
                client.ask('题目')

    def test_missing_api_key_raises_config_error(self):
        with self.assertRaises(LLMConfigError):
            make_client(make_settings('https://example.com/v1', api_key=''))

    def test_extra_body_is_opt_in(self):
        with FakeLLMServer() as server:
            # 默认不注入任何非标准字段（严格网关会 400）
            client = make_client(make_settings(f'{server.base}/v1', stream=False))
            client.ask('题目')
            body = server.state['last_body']
            self.assertNotIn('thinking', body)
            self.assertNotIn('temperature', body)
            self.assertEqual(body['model'], 'fake-model')
            self.assertFalse(body['stream'])

            # 用户显式配置才会带上
            client = make_client(make_settings(
                f'{server.base}/v1', stream=False,
                extra_body={'thinking': {'type': 'disabled'}, 'temperature': 0},
            ))
            client.ask('题目')
            body = server.state['last_body']
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['temperature'], 0)

    def test_structured_logging_contains_resolved_url(self):
        from tools.logger import logger as llm_logger

        records = []
        sink_id = llm_logger.add(lambda message: records.append(str(message)),
                                format='{message}', level='INFO')
        try:
            with FakeLLMServer() as server:
                client = make_client(make_settings(f'{server.base}/v1'))
                client.ask('题目')
                expected_url = f'{server.base}/v1/chat/completions'
        finally:
            llm_logger.remove(sink_id)

        joined = '\n'.join(records)
        self.assertIn('[LLM] req#', joined)
        self.assertIn('[LLM] resp#', joined)
        self.assertIn(f'url={expected_url}', joined)
        self.assertIn('chars=', joined)


if __name__ == '__main__':
    unittest.main()
