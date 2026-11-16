#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""B站答题会话

修复要点（P0）：
1. 模型调用失败（超时/限流/网络抖动）不再直接结束整场答题：
   - 连续 3 次底层调用失败才停止（并提示检查网络或超时设置）
   - 单次失败按指数退避重试，且**重试同一道题**，不会白白浪费题目
2. 模型回复解析不出选项序号时重问同一题，最多 3 次再跳过该题（原实现会无限重问同一题）
3. 答案解析改用正则，兼容 '3'、'答案：3'、'选项 2'、'3.'、全角数字等格式
4. 不再持有 `from config.config import model_choice` 的导入期快照，
   模型选择只由 `provider` 一个真值来源决定
5. 依赖可注入（provider / ask / fetch_question / submit），便于无网络单元测试
"""

from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple

from client.senior import captcha_get, captcha_submit, category_get, question_get, question_submit
from tools.LLM.client import (LLMClient, LLMConfigError, LLMError, create_client,
                              normalize_provider, parse_choice)
from tools.logger import logger

#: 一场答题最多处理多少题
MAX_TOTAL_QUESTIONS = 100
#: 同一道题最多问模型几次（解析失败才重问）
MAX_ASK_ATTEMPTS_PER_QUESTION = 3
#: 连续多少次底层调用失败才放弃整场答题
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3
#: 连续多少次"整题跳过"就停止（防止服务端一直返回同一题造成死循环）
MAX_CONSECUTIVE_SKIPS = 5


class QuizSession:
    def __init__(self,
                 provider: Any = 'deepseek',
                 client: Optional[LLMClient] = None,
                 ask: Optional[Callable[[str], str]] = None,
                 fetch_question: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 submit: Optional[Callable[..., Optional[Dict[str, Any]]]] = None):
        self.question_id: Optional[Any] = None
        self.answers: Optional[List[Dict[str, Any]]] = None
        self.question_num: int = 0
        self.question: Optional[str] = None
        self.stopped: bool = False

        #: 当前使用的模型（provider 名；'1'/'2'/'3' 会被归一化）
        self.provider: str = normalize_provider(provider)
        self._client = client
        self._client_provider: Optional[str] = None

        #: 可注入的依赖（默认走真实实现）
        self.ask_fn = ask
        self.fetch_question = fetch_question or question_get
        self.submit_fn = submit or question_submit

        self._ask_attempts = 0
        self._consecutive_skips = 0

    # ==================== 模型调用 ====================

    def _get_client(self) -> LLMClient:
        if self._client is None or self._client_provider != self.provider:
            self._client = create_client(self.provider)
            self._client_provider = self.provider
        return self._client

    def ask_model(self, prompt: str) -> str:
        """向模型提问（测试可注入 ask_fn 绕开网络）"""
        if self.ask_fn is not None:
            return self.ask_fn(prompt)
        return self._get_client().ask(prompt)

    def update_model_choice(self, new_model_choice: Any) -> None:
        """更新当前使用的模型

        Args:
            new_model_choice: '1'/'2'/'3' 或 'deepseek'/'gemini'/'custom'
        """
        provider = normalize_provider(new_model_choice)
        if provider == self.provider:
            return
        self.provider = provider
        self._client = None
        self._client_provider = None
        logger.info(f"已切换模型为: {provider}")

    # ==================== 主循环 ====================

    def start(self) -> None:
        """开始答题会话"""
        transport_failures = 0
        try:
            while not self.stopped and self.question_num < MAX_TOTAL_QUESTIONS:
                if self.question is None:
                    if not self.get_question():
                        logger.error("获取题目失败，答题结束")
                        return
                    self._ask_attempts = 0
                    if self.stopped:
                        break

                self.display_question()
                choice, error_kind = self._ask_for_choice()

                if error_kind == 'fatal':
                    logger.error("模型配置有问题，答题结束")
                    return

                if error_kind == 'transport':
                    transport_failures += 1
                    if transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                        logger.error(
                            f"连续 {transport_failures} 次调用模型失败，答题结束。"
                            f"请检查网络/代理，或在设置页调大读取超时后重试"
                        )
                        return
                    wait = min(2 ** (transport_failures - 1), 8)
                    logger.warning(f"第{self.question_num}题调用模型失败，{wait}s 后重试同一题")
                    sleep(wait)
                    continue

                if choice is None:
                    self._ask_attempts += 1
                    if self._ask_attempts >= MAX_ASK_ATTEMPTS_PER_QUESTION:
                        logger.error(
                            f"第{self.question_num}题连续 {self._ask_attempts} 次无法得到有效选项，跳过该题"
                        )
                        self._reset_question()
                        self._consecutive_skips += 1
                        if self._consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
                            logger.error(f"连续 {self._consecutive_skips} 题解析失败，答题结束")
                            return
                        continue
                    logger.warning(
                        f"第{self.question_num}题回复无法解析选项，重问同一题"
                        f"（{self._ask_attempts}/{MAX_ASK_ATTEMPTS_PER_QUESTION}）"
                    )
                    sleep(1)
                    continue

                transport_failures = 0
                self._consecutive_skips = 0

                if not self.submit_answer(self.answers[choice - 1]):
                    logger.error("提交答案失败，答题结束")
                    return
                self._reset_question()

        except KeyboardInterrupt:
            logger.info("答题会话已终止")
        except Exception as e:
            logger.exception(f"答题过程发生错误: {type(e).__name__}: {e}")

    def _ask_for_choice(self) -> Tuple[Optional[int], Optional[str]]:
        """询问模型并解析选项序号

        Returns:
            (序号, 错误类型)。错误类型为 None 表示成功；
            'transport' 表示调用层面失败；'parse' 表示回复无法解析；'fatal' 表示配置错误。
        """
        if self.stopped:
            return None, None
        try:
            text = self.ask_model(self.get_question_prompt())
        except LLMConfigError as e:
            logger.error(f"[LLM] 配置错误: {e}")
            return None, 'fatal'
        except LLMError as e:
            logger.error(f"[LLM] 第{self.question_num}题调用失败（{type(e).__name__}）: {e}")
            return None, 'transport'
        except Exception as e:
            logger.error(f"[LLM] 第{self.question_num}题调用异常（{type(e).__name__}）: {e}")
            return None, 'transport'

        reply = '' if text is None else str(text).strip()
        logger.info(f"[LLM] 第{self.question_num}题回复（{len(reply)}字符）: {reply[:200]!r}")

        choice = parse_choice(reply, len(self.answers or []))
        if choice is None:
            logger.warning(f"[LLM] 第{self.question_num}题无法从回复中解析出选项序号")
            return None, 'parse'

        logger.info(f"AI给出的答案: {choice}")
        return choice, None

    def _reset_question(self) -> None:
        """清空当前题目状态，下一轮重新取题"""
        self.question = None
        self.answers = None
        self.question_id = None
        self._ask_attempts = 0

    # ==================== B站接口 ====================

    def get_question(self) -> bool:
        """获取题目

        Returns:
            bool: 是否成功获取题目
        """
        try:
            question = self.fetch_question()
            if not question:
                return False

            if question.get('code') != 0:
                logger.info("需要验证码验证")
                return self.handle_verification()

            data = question.get('data', {}) or {}
            self.question = data.get('question')
            self.answers = data.get('answers', []) or []
            self.question_id = data.get('id')
            self.question_num = data.get('question_num', 0) or 0
            return True

        except Exception as e:
            logger.error(f"获取题目失败: {str(e)}")
            return False

    def handle_verification(self) -> bool:
        """处理验证码验证（GUI 线程会整体替换此方法）

        Returns:
            bool: 验证是否成功
        """
        try:
            if self.stopped:
                logger.info("答题已停止")
                return False

            logger.info("获取分类信息...")
            category = category_get()
            if not category:
                return False
            if self.stopped:
                logger.info("答题已停止")
                return False

            logger.info("分类信息:")
            for cat in category.get('categories', []):
                logger.info(f"ID: {cat.get('id')} - {cat.get('name')}")
            logger.info("tips: 输入多个分类ID请用 *英文逗号* 隔开,例如:1,2,3")
            ids = input('请输入分类ID: ')

            if self.stopped:
                logger.info("答题已停止")
                return False

            logger.info("获取验证码...")
            captcha_res = captcha_get()
            if not captcha_res:
                return False
            logger.info("请打开链接查看验证码内容:{}".format(captcha_res.get('url')))

            if self.stopped:
                logger.info("答题已停止")
                return False

            captcha = input('请输入验证码: ')

            if captcha_submit(code=captcha, captcha_token=captcha_res.get('token'), ids=ids):
                logger.info("验证通过✅")
                return self.get_question()
            logger.error("验证失败")
            return False

        except Exception as e:
            logger.error(f"验证过程发生错误: {str(e)}")
            return False

    def display_question(self) -> None:
        """显示当前题目和选项"""
        if not self.answers:
            logger.warning("没有可用的题目")
            return

        logger.info(f"第{self.question_num}题:{self.question}")
        for i, answer in enumerate(self.answers, 1):
            logger.info(f"{i}. {answer.get('ans_text')}")

    def get_question_prompt(self) -> str:
        return '''
        题目:{}
        答案:{}
        '''.format(self.question, self.answers)

    def submit_answer(self, answer: Dict[str, Any]) -> bool:
        """提交答案

        Args:
            answer: 答案信息

        Returns:
            bool: 是否成功提交答案
        """
        try:
            result = self.submit_fn(
                self.question_id,
                answer.get('ans_hash'),
                answer.get('ans_text')
            )
            if result and result.get('code') == 0:
                logger.info("答案提交成功")
                sleep(1)
                return True
            logger.error(f"答案提交失败: {result}")
            return False
        except Exception as e:
            logger.error(f"提交答案时发生错误: {str(e)}")
            return False


# 创建答题会话实例（GUI 与命令行共用）
quiz_session = QuizSession()


def start() -> None:
    """启动答题程序"""
    quiz_session.start()
    logger.info('答题结束')
