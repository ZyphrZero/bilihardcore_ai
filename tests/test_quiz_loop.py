#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""答题主循环的单元测试。

**安全约束**：这些测试绝不访问 B 站接口，也不会真的调用任何模型服务。
- 题目获取/答案提交全部注入假函数
- 每个用例都 patch 掉 `client.senior.get/post` 与 `tools.request_b.get/post`，
  一旦有代码试图真的发 B 站请求就会 AssertionError 立刻失败
- 需要"真客户端"的用例只连本机 127.0.0.1 的假 LLM 服务
"""

import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.start_senior import QuizSession  # noqa: E402
from tests.fake_llm_server import FakeLLMServer  # noqa: E402
from tools.LLM.client import (LLMClient, LLMSettings,  # noqa: E402
                              LLMTimeoutError)

FORBIDDEN = '测试禁止访问 B 站接口'


def question_payload(num, answers=('甲', '乙', '丙', '丁')):
    return {
        'code': 0,
        'data': {
            'question': f'第{num}题的题干',
            'answers': [{'ans_hash': f'h{num}{i}', 'ans_text': text}
                        for i, text in enumerate(answers, 1)],
            'id': 1000 + num,
            'question_num': num,
        },
    }


class FakeBili:
    """假题目源 + 假提交接口（记录调用次数）"""

    def __init__(self, total_questions):
        self.total_questions = total_questions
        self.fetch_count = 0   # 取题接口被调用次数（含最后一次返回 None）
        self.served = 0        # 真正返回了题目的次数
        self.submitted = []

    def fetch(self):
        self.fetch_count += 1
        if self.fetch_count > self.total_questions:
            return None  # 题源耗尽 → 主循环正常结束
        self.served += 1
        return question_payload(self.fetch_count)

    def submit(self, question_id, ans_hash, ans_text):
        self.submitted.append((question_id, ans_hash, ans_text))
        return {'code': 0}


class QuizSessionTestBase(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch('scripts.start_senior.sleep', lambda *a, **k: None),
            mock.patch('client.senior.get', side_effect=AssertionError(FORBIDDEN)),
            mock.patch('client.senior.post', side_effect=AssertionError(FORBIDDEN)),
            mock.patch('tools.request_b.get', side_effect=AssertionError(FORBIDDEN)),
            mock.patch('tools.request_b.post', side_effect=AssertionError(FORBIDDEN)),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_session(self, ask, total_questions=1):
        bili = FakeBili(total_questions)
        session = QuizSession(provider='custom', ask=ask,
                              fetch_question=bili.fetch, submit=bili.submit)
        return session, bili


class QuizLoopTests(QuizSessionTestBase):
    def test_answers_three_questions_then_ends(self):
        replies = iter(['1', '答案：2', '3'])
        session, bili = self.make_session(lambda _prompt: next(replies), total_questions=3)

        session.start()

        self.assertEqual(len(bili.submitted), 3)
        self.assertEqual([item[2] for item in bili.submitted], ['甲', '乙', '丙'])
        # 3 题各取一次，第 4 次取到 None 结束
        self.assertEqual(bili.fetch_count, 4)

    def test_unparsable_reply_reasks_same_question(self):
        replies = iter(['我不知道', '答案：2'])
        session, bili = self.make_session(lambda _prompt: next(replies))

        session.start()

        # 同一道题只取了一次，但问了模型两次
        self.assertEqual(bili.served, 1)
        self.assertEqual(len(bili.submitted), 1)
        self.assertEqual(bili.submitted[0][2], '乙')

    def test_skips_question_after_three_parse_failures(self):
        replies = iter(['不知道', '不知道', '不知道', '1'])
        session, bili = self.make_session(lambda _prompt: next(replies), total_questions=2)

        session.start()

        # 第 1 题被跳过（没有提交），第 2 题正常提交
        self.assertEqual(len(bili.submitted), 1)
        self.assertEqual(bili.submitted[0][0], 1002)  # 第 2 题的 id
        self.assertEqual(bili.fetch_count, 3)          # 第1题、第2题、(None)

    def test_recovers_from_transient_llm_failures_with_same_question(self):
        state = {'calls': 0}

        def flaky_ask(_prompt):
            state['calls'] += 1
            if state['calls'] <= 2:
                raise LLMTimeoutError('读取超时（模拟）')
            return '1'

        session, bili = self.make_session(flaky_ask)

        session.start()

        self.assertEqual(state['calls'], 3)
        # 重试的是同一道题，没有浪费题目
        self.assertEqual(bili.served, 1)
        self.assertEqual(len(bili.submitted), 1)

    def test_stops_after_three_consecutive_llm_failures(self):
        state = {'calls': 0}

        def always_fail(_prompt):
            state['calls'] += 1
            raise LLMTimeoutError('读取超时（模拟）')

        session, bili = self.make_session(always_fail)

        session.start()

        self.assertEqual(state['calls'], 3)
        self.assertEqual(bili.submitted, [])
        self.assertEqual(bili.served, 1)

    def test_submit_failure_stops_session(self):
        def failing_submit(question_id, ans_hash, ans_text):
            return {'code': -1, 'message': '提交失败'}

        bili = FakeBili(3)
        session = QuizSession(provider='custom', ask=lambda _p: '1',
                              fetch_question=bili.fetch, submit=failing_submit)
        session.start()

        self.assertEqual(bili.served, 1)


class ModelChoiceTests(QuizSessionTestBase):
    def test_update_model_choice_accepts_choice_values(self):
        session, _bili = self.make_session(lambda _p: '1')
        self.assertEqual(session.provider, 'custom')

        session.update_model_choice('2')
        self.assertEqual(session.provider, 'gemini')

        session.update_model_choice('1')
        self.assertEqual(session.provider, 'deepseek')

        session.update_model_choice('deepseek')
        self.assertEqual(session.provider, 'deepseek')


class EndToEndWithFakeServerTests(QuizSessionTestBase):
    def test_real_client_streams_answer_and_submits(self):
        """完整链路：SSE 流式 → 解析选项 → 提交（只连本机假服务）"""
        with FakeLLMServer() as server:
            settings = LLMSettings(provider='custom', base_url=f'{server.base}/v1',
                                   model='fake-model', api_key='sk-test',
                                   connect_timeout=2.0, read_timeout=5.0,
                                   stream=True, max_retries=0)
            http_session = requests.Session()
            http_session.trust_env = False
            client = LLMClient(settings, session=http_session)

            bili = FakeBili(1)
            quiz = QuizSession(provider='custom', ask=client.ask,
                               fetch_question=bili.fetch, submit=bili.submit)
            quiz.start()

        self.assertEqual(len(bili.submitted), 1)
        # fake 服务固定返回"答案：3"
        self.assertEqual(bili.submitted[0][2], '丙')
        self.assertEqual(server.count('v1'), 1)


if __name__ == '__main__':
    unittest.main()
