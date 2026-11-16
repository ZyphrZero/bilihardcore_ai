#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""DeepSeek 适配器（实际的请求逻辑在 tools/LLM/client.py）"""

from typing import Optional

from tools.LLM.client import LLMClient, create_client


class DeepSeekAPI:
    """DeepSeek 官方 API（OpenAI 兼容协议）"""

    provider = 'deepseek'

    def __init__(self, client: Optional[LLMClient] = None, **overrides):
        self._client = client or create_client(self.provider, **overrides)

    @property
    def client(self) -> LLMClient:
        return self._client

    def ask(self, question: str, timeout: Optional[float] = None) -> str:
        """向模型提问，返回模型回复的原始文本"""
        return self._client.ask(question, timeout=timeout)
