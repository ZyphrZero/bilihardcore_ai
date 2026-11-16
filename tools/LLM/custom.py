#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""自定义 / 中转站模型适配器（OpenAI 兼容协议）

修复历史问题：
- base_url 归一化有明确规则，不再靠结尾 '/'、'#' 猜（'#' 结尾仍表示"我给的就是完整地址"）
- 不再硬编码 thinking / temperature（非标准字段会被严格网关拒绝），
  需要时通过 extra_body 显式传入
- 不再用 print 输出调试信息（打包成无控制台程序后会静默丢失）
"""

from typing import Any, Dict, Optional

from tools.LLM.client import LLMClient, LLMSettings, create_client, resolve_endpoint


class APIUtils:
    """保留的工具类：把 Base URL 归一化成完整请求地址"""

    @staticmethod
    def format_api_url(base_url: str, model: str = '', stream: bool = False) -> str:
        settings = LLMSettings(provider='custom', base_url=base_url, model=model,
                               api_key='x', stream=stream)
        return resolve_endpoint(settings)


class CustomAPI:
    """任意 OpenAI 兼容的中转站 / 云厂商"""

    provider = 'custom'

    def __init__(self, client: Optional[LLMClient] = None, **overrides):
        self._client = client or create_client(self.provider, **overrides)

    @property
    def client(self) -> LLMClient:
        return self._client

    def ask(self, question: str, timeout: Optional[float] = None) -> str:
        """向模型提问，返回模型回复的原始文本"""
        return self._client.ask(question, timeout=timeout)

    # 兼容旧调用方 -------------------------------------------------------
    def ask_openai_format(self, question: str, timeout: Optional[float] = None) -> str:
        return self.ask(question, timeout=timeout)

    def ask_dashscope_format(self, question: str, timeout: Optional[float] = None) -> str:
        return self.ask(question, timeout=timeout)

    def ask_custom_format(self, question: str, timeout: Optional[float] = None) -> str:
        return self.ask(question, timeout=timeout)
