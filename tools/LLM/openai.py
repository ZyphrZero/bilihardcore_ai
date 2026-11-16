#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""OpenAI 官方 / 任意 OpenAI 兼容端点适配器

历史问题：本模块原先从 config.config 导入 API_KEY_OPENAI / BASE_URL_OPENAI /
MODEL_OPENAI，但这三个名字并不存在，一旦被 import 就抛 ImportError。
现在改为显式传参，未传时退回 config 里的 custom 配置。
"""

from typing import Optional

from tools.LLM.client import LLMClient, LLMSettings, build_settings


class OpenAIAPI:
    """OpenAI 兼容端点（默认使用设置页里的"自定义模型"配置）"""

    provider = 'custom'

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 api_key: Optional[str] = None, client: Optional[LLMClient] = None,
                 **overrides):
        if client is not None:
            self._client = client
            return
        settings = build_settings(
            self.provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            **overrides,
        )
        self._client = LLMClient(settings)

    @property
    def settings(self) -> LLMSettings:
        return self._client.settings

    def ask(self, question: str, timeout: Optional[float] = None) -> str:
        """向模型提问，返回模型回复的原始文本"""
        return self._client.ask(question, timeout=timeout)
