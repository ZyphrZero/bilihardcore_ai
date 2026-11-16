#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Gemini 适配器（实际的请求逻辑在 tools/LLM/client.py）

使用 Gemini 原生协议：
- 非流式：/models/{model}:generateContent
- 流式：  /models/{model}:streamGenerateContent?alt=sse

Base URL 只需要填到版本号，例如 https://generativelanguage.googleapis.com/v1beta；
如果只填了域名，客户端会自动补 /v1beta（旧版本这里会拼出 404 的地址）。
"""

from typing import Optional

from tools.LLM.client import LLMClient, create_client


class GeminiAPI:
    """Google Gemini 原生 API"""

    provider = 'gemini'

    def __init__(self, client: Optional[LLMClient] = None, **overrides):
        self._client = client or create_client(self.provider, **overrides)

    @property
    def client(self) -> LLMClient:
        return self._client

    def ask(self, question: str, timeout: Optional[float] = None) -> str:
        """向模型提问，返回模型回复的原始文本"""
        return self._client.ask(question, timeout=timeout)
