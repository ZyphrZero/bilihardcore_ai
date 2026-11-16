#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
统一的 LLM 客户端

设计目标（解决历史问题）：
1. 超时拆成 (连接超时, 读取超时)。requests 的 timeout 是"相邻两次收到字节"的间隔，
   而不是整段请求的总时长；只要流式输出不断有数据，就不会触发读超时。
2. 默认开启流式（SSE），避免中转站"攒完再发"把整段生成时间压在读超时上。
3. 失败重试：只对连接/读取超时、连接错误、408/409/425/429/5xx 做指数退避重试；
   4xx 鉴权/参数错误立即失败。
4. 结构化日志：每一次尝试都记录 请求 URL / 模型 / 序号 / 耗时 / 首字时间 / 状态码 / usage，
   便于和模型提供商后台的计费记录对齐。
5. 流式过程中如果已经收到部分内容后超时，直接返回已收到的内容（标记 partial），
   避免"上游已计费、本地却判失败"进而重复请求重复计费。
6. URL 归一化有明确规则，不再靠结尾 '/', '#' 之类的隐式约定猜。

支持两种协议：
- openai: OpenAI 兼容协议（DeepSeek、各类中转站、阿里云百炼兼容模式、OpenAI 官方）
- gemini: Google Gemini 原生协议（支持 :streamGenerateContent?alt=sse 流式）
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from config.config import PROMPT, MODEL_CONFIGS, load_model_config, load_llm_options
from tools.logger import logger


# ==================== 异常 ====================


class LLMError(Exception):
    """LLM 调用相关错误的基类"""


class LLMConfigError(LLMError):
    """配置错误（密钥/地址/模型名为空、格式不对）"""


class LLMHTTPError(LLMError):
    """服务端返回了非 2xx 状态码"""

    def __init__(self, message: str, status: Optional[int] = None,
                 body: str = '', retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


class LLMTimeoutError(LLMError):
    """连接/读取超时（已按配置重试过）"""


class LLMResponseError(LLMError):
    """响应体不是预期的结构/不是合法 JSON"""


# ==================== 配置 ====================

#: provider -> 协议与默认值
PRESETS: Dict[str, Dict[str, Any]] = {
    'deepseek': {'dialect': 'openai', 'label': 'DeepSeek'},
    'gemini': {'dialect': 'gemini', 'label': 'Gemini'},
    'custom': {'dialect': 'openai', 'label': '自定义模型'},
}

#: GUI 下拉框/单选按钮的取值 -> provider
CHOICE_TO_PROVIDER = {'1': 'deepseek', '2': 'gemini', '3': 'custom'}
PROVIDER_TO_CHOICE = {v: k for k, v in CHOICE_TO_PROVIDER.items()}

#: 这些状态码值得重试
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def normalize_provider(value: Any) -> str:
    """把 '1'/'2'/'3' 或 'DeepSeek' 之类的输入统一成 provider 名。"""
    if value is None:
        return 'deepseek'
    text = str(value).strip()
    if text in CHOICE_TO_PROVIDER:
        return CHOICE_TO_PROVIDER[text]
    lowered = text.lower()
    if lowered in PRESETS:
        return lowered
    return 'deepseek'


def _looks_like_read_timeout(exc: BaseException) -> bool:
    """requests 在流式读取超时时会把 urllib3 的 ReadTimeoutError 包装成 ConnectionError，
    这里沿异常链判断一下，保证日志里区分"连不上"和"读超时"。"""
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if 'readtimeouterror' in text or 'read timed out' in text:
            return True
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
    return False


@dataclass
class LLMSettings:
    """一次 LLM 调用所需的全部设置"""

    provider: str = 'deepseek'
    base_url: str = ''
    model: str = ''
    api_key: str = ''
    dialect: str = 'openai'
    connect_timeout: float = 10.0
    read_timeout: float = 180.0
    stream: bool = True
    max_retries: int = 2
    temperature: Optional[float] = None
    extra_body: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """返回配置错误列表，空列表表示可用。"""
        errors: List[str] = []
        label = PRESETS.get(self.provider, {}).get('label', self.provider)
        if not self.api_key:
            errors.append(f'{label} 的 API Key 为空')
        if not self.base_url:
            errors.append(f'{label} 的 API Base URL 为空')
        elif not self.base_url.startswith(('http://', 'https://')):
            errors.append(f'{label} 的 API Base URL 必须以 http:// 或 https:// 开头')
        if not self.model:
            errors.append(f'{label} 的模型名称为空')
        if self.dialect not in ('openai', 'gemini'):
            errors.append(f'未知的协议类型: {self.dialect}')
        return errors

    def validate_or_raise(self) -> None:
        errors = self.validate()
        if errors:
            raise LLMConfigError('；'.join(errors) + '（请在设置页保存后再试）')


# ==================== URL 归一化 ====================


def _strip_trailing(url: str) -> str:
    return url.strip().rstrip('/')


def resolve_endpoint(settings: LLMSettings) -> str:
    """根据协议把 Base URL 归一化成完整的请求地址。

    规则（openai 协议）：
    - 以 '#' 结尾：用户给的就是完整 endpoint，原样使用（历史约定，保留兼容）
    - 已经包含 '/chat/completions'：原样使用
    - 阿里云 host：补 '/compatible-mode/v1/chat/completions'
    - 以 '/v1' 结尾：补 '/chat/completions'
    - 其它（含只有域名的情况）：补 '/v1/chat/completions'

    规则（gemini 协议）：
    - 以 '#' 结尾 / 已包含 ':generateContent'：视为完整地址
    - 否则补 '/models/{model}:streamGenerateContent?alt=sse'（流式）或 ':generateContent'
    - Base URL 缺少版本段（/v1beta、/v1、/v1alpha）时自动补 '/v1beta'，
      避免用户照占位符填了 'https://generativelanguage.googleapis.com' 就 404
    """
    raw = (settings.base_url or '').strip()
    if not raw:
        raise LLMConfigError('API Base URL 为空')

    if raw.endswith('#'):
        return raw[:-1].strip()

    base = _strip_trailing(raw)
    lowered = base.lower()

    if settings.dialect == 'gemini':
        if ':generatecontent' in lowered:
            if settings.stream and 'alt=sse' not in lowered:
                return base + ('&' if '?' in base else '?') + 'alt=sse'
            return base
        without_query = base.split('?')[0].rstrip('/')
        path = without_query.split('/', 3)
        path_part = path[3] if len(path) > 3 else ''
        if not re.search(r'/v1(alpha|beta)?(/|$)', '/' + path_part):
            without_query = without_query + '/v1beta'
        action = 'streamGenerateContent' if settings.stream else 'generateContent'
        url = f'{without_query}/models/{settings.model}:{action}'
        if settings.stream:
            url += '?alt=sse'
        return url

    # openai 协议
    if '/chat/completions' in lowered:
        return base
    if 'aliyuncs.com' in lowered and '/compatible-mode' not in lowered:
        return base + '/compatible-mode/v1/chat/completions'
    if lowered.endswith('/v1') or lowered.endswith('/compatible-mode/v1'):
        return base + '/chat/completions'
    return base + '/v1/chat/completions'


# ==================== 答案解析 ====================


_DIGIT_TRANS = str.maketrans('０１２３４５６７８９', '0123456789')
_STANDALONE_DIGIT = re.compile(r'(?<![0-9])([0-9])(?![0-9])')

#: 明确的"结论"标记：答案：3 / 正确选项 2 / 应选 4 / 选择 1 / Answer: 3
_ANSWER_MARKER = re.compile(
    r'(?:答案|正确选项|应选|应该选|最终选择|选项|选择|选|answer|option)'
    r'\s*(?:是|为|:|：|第)?\s*'
    r'(?<![0-9])([0-9])(?![0-9])',
    re.IGNORECASE,
)


def parse_choice(text: Any, max_choice: int) -> Optional[int]:
    """从模型回复里解析选项序号。

    兼容 '3'、'答案：3'、'选项 2'、'3.'、全角数字等情况；
    '12' 这种连写的多位数字不会被误判成 1 或 2。

    带思维链的模型（deepseek-reasoner、Qwen3-thinking、gpt-5 等）会在正文里
    先罗列"1. xxx  2. xxx"再给结论，旧实现取**第一个**数字，于是几乎必然
    取到选项 1。这里改为：
    1. 优先找"答案/应选/正确选项"这类标记后的数字，取**最后一个**；
    2. 没有标记时退化为独立数字，同样取**最后一个**（结论通常在末尾）。

    返回 1..max_choice 之间的整数，解析失败返回 None。
    """
    if text is None:
        return None
    if max_choice <= 0:
        return None
    normalized = str(text).translate(_DIGIT_TRANS)

    def _in_range(digit: str) -> Optional[int]:
        try:
            value = int(digit)
        except (TypeError, ValueError):  # pragma: no cover - 正则已保证是数字
            return None
        return value if 1 <= value <= max_choice else None

    for match in reversed(list(_ANSWER_MARKER.finditer(normalized))):
        value = _in_range(match.group(1))
        if value is not None:
            return value

    for match in reversed(list(_STANDALONE_DIGIT.finditer(normalized))):
        value = _in_range(match.group(1))
        if value is not None:
            return value

    return None


# ==================== 客户端 ====================


class LLMClient:
    """线程内可复用的 LLM 客户端（复用一个 requests.Session）"""

    def __init__(self, settings: LLMSettings, session: Optional[requests.Session] = None,
                 prompt_template: str = PROMPT, timeout: Optional[float] = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.prompt_template = prompt_template
        #: 兼容旧接口：显式传入的 timeout 覆盖读取超时
        self.timeout_override = timeout
        settings.validate_or_raise()
        self.url = resolve_endpoint(settings)

    # ---------- 内部工具 ----------

    def _build_prompt(self, question: str) -> str:
        try:
            return self.prompt_template.format(time.strftime('%Y-%m-%d %H:%M:%S'), question)
        except (IndexError, KeyError):
            return f'{question}'

    def _build_payload(self, question: str) -> Dict[str, Any]:
        prompt = self._build_prompt(question)
        if self.settings.dialect == 'gemini':
            payload: Dict[str, Any] = {'contents': [{'parts': [{'text': prompt}]}]}
            generation: Dict[str, Any] = {}
            if self.settings.temperature is not None:
                generation['temperature'] = self.settings.temperature
            if generation:
                payload['generationConfig'] = generation
            return payload

        payload = {
            'model': self.settings.model,
            'messages': [{'role': 'user', 'content': prompt}],
            # 显式声明，避免个别网关在缺省时按自己的默认值处理
            'stream': bool(self.settings.stream),
        }
        if self.settings.temperature is not None:
            payload['temperature'] = self.settings.temperature
        # 额外参数（例如某些中转站的 thinking 开关）由用户显式配置，
        # 不再硬编码，避免被严格网关判定为非法字段而 400。
        for key, value in (self.settings.extra_body or {}).items():
            payload[key] = value
        return payload

    def _build_headers(self, request_id: str) -> Dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'X-Request-Id': request_id,
            'Accept': 'text/event-stream' if self.settings.stream else 'application/json',
        }
        if self.settings.dialect == 'gemini':
            headers['x-goog-api-key'] = self.settings.api_key
        else:
            headers['Authorization'] = f'Bearer {self.settings.api_key}'
        return headers

    @staticmethod
    def _short(text: Any, limit: int = 300) -> str:
        value = str(text)
        value = value.replace('\n', ' ')
        return value if len(value) <= limit else value[:limit] + '…'

    def _raise_for_status(self, response: requests.Response, request_id: str) -> None:
        if response.status_code < 400:
            return
        try:
            body = response.text
        except Exception:  # pragma: no cover - 极少数流未读取的场景
            body = ''
        retryable = response.status_code in RETRYABLE_STATUS
        logger.error(
            f'[LLM] fail#{request_id} status={response.status_code} '
            f'retryable={retryable} body={self._short(body)}'
        )
        raise LLMHTTPError(
            f'HTTP {response.status_code}: {self._short(body, 200)}',
            status=response.status_code,
            body=body,
            retryable=retryable,
        )

    def _extract_stream_piece(self, event: Dict[str, Any]) -> str:
        if self.settings.dialect == 'gemini':
            for candidate in event.get('candidates') or []:
                content = candidate.get('content') or {}
                for part in content.get('parts') or []:
                    if part.get('thought'):
                        # Gemini 的"思考摘要"也放在 parts 里，带 thought=true。
                        # 混进正文会让答案解析踩到推理里的数字，这里跳过。
                        continue
                    text = part.get('text')
                    if text:
                        return text
            return ''

        choices = event.get('choices') or []
        if not choices:
            return ''
        choice = choices[0] or {}
        delta = choice.get('delta') or {}
        if delta.get('content'):
            return delta['content']
        message = choice.get('message') or {}
        if message.get('content'):
            return message['content']
        return ''

    def _extract_reasoning_piece(self, event: Dict[str, Any]) -> str:
        """取出"思维链"增量。

        deepseek-reasoner 用 `reasoning_content`，OpenRouter/部分网关用 `reasoning`，
        Gemini 用 parts[].thought=true。正常情况这些内容不进正文，只在
        模型只吐思考、正文为空时兜底使用。
        """
        if self.settings.dialect == 'gemini':
            texts = []
            for candidate in event.get('candidates') or []:
                content = candidate.get('content') or {}
                for part in content.get('parts') or []:
                    if part.get('thought') and part.get('text'):
                        texts.append(part['text'])
            return ''.join(texts)

        for choice in event.get('choices') or []:
            choice = choice or {}
            for holder_key in ('delta', 'message'):
                holder = choice.get(holder_key) or {}
                for key in ('reasoning_content', 'reasoning'):
                    value = holder.get(key)
                    if isinstance(value, str) and value:
                        return value
        return ''

    def _extract_full_text(self, event: Dict[str, Any]) -> str:
        if self.settings.dialect == 'gemini':
            texts = []
            thought_texts = []
            for candidate in event.get('candidates') or []:
                content = candidate.get('content') or {}
                for part in content.get('parts') or []:
                    if not part.get('text'):
                        continue
                    if part.get('thought'):
                        thought_texts.append(part['text'])
                    else:
                        texts.append(part['text'])
            # 只有思考没有正文时退回思考内容（否则整题白问一次）
            return ''.join(texts) or ''.join(thought_texts)

        choices = event.get('choices') or []
        if not choices:
            raise LLMResponseError(f'响应中没有 choices 字段: {self._short(event, 200)}')
        choice = choices[0] or {}
        message = choice.get('message') or {}
        if message.get('content') is not None:
            return message['content']
        if choice.get('text') is not None:
            return choice['text']
        for key in ('reasoning_content', 'reasoning'):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
            value = choice.get(key)
            if isinstance(value, str) and value:
                return value
        raise LLMResponseError(f'响应中没有 message.content: {self._short(event, 200)}')

    # ---------- 单次请求 ----------

    def _post(self, payload: Dict[str, Any], headers: Dict[str, str],
              read_timeout: float) -> requests.Response:
        timeout = (max(0.1, float(self.settings.connect_timeout)), max(0.1, float(read_timeout)))
        return self.session.post(
            self.url,
            headers=headers,
            json=payload,
            timeout=timeout,
            stream=self.settings.stream,
        )

    def _ask_stream(self, payload: Dict[str, Any], headers: Dict[str, str], request_id: str,
                    read_timeout: float, started: float) -> Tuple[str, Dict[str, Any]]:
        meta: Dict[str, Any] = {
            'status': None, 'ttft': None, 'usage': None, 'provider_id': None,
            'partial': False, 'chunks': 0, 'reason': '',
        }
        response = self._post(payload, headers, read_timeout)
        meta['status'] = response.status_code
        try:
            self._raise_for_status(response, request_id)

            # 有些中转站/网关会忽略 "stream": true，直接返回一个完整的 JSON。
            # 这在旧实现里会表现为"请求成功但拿不到任何内容"，进而被当成
            # 模型答非所问，白白重试几次。这里识别出来，走非流式解析。
            content_type = (response.headers.get('Content-Type') or '').lower()
            if 'event-stream' not in content_type and 'json' in content_type:
                logger.warning(
                    f'[LLM] stream-ignored#{request_id} 服务端返回 {content_type or "?"}，'
                    f'未按流式输出，改用整体响应解析'
                )
                text, meta = self._read_full_body(response, request_id, meta)
                return text, meta

            pieces: List[str] = []
            reasoning_parts: List[str] = []
            raw_lines: List[bytes] = []
            try:
                # chunk_size=1：让 iter_lines 立刻把已经到达的 SSE 行交出来。
                # 用大 chunk_size 时 urllib3/http.client 会攒够字节数才返回，
                # 小事件（每个 token 一行）就会被攒在缓冲区里，读超时判断也会因此变钝。
                for raw_line in response.iter_lines(chunk_size=1, decode_unicode=False):
                    if raw_line is None:
                        continue
                    raw_lines.append(raw_line)
                    line = raw_line.decode('utf-8', 'replace') if isinstance(raw_line, bytes) else raw_line
                    line = line.strip()
                    if not line or line.startswith(':'):
                        continue
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if not data or data == '[DONE]':
                        if data == '[DONE]':
                            break
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if meta['provider_id'] is None and event.get('id'):
                        meta['provider_id'] = event['id']
                    if event.get('usage'):
                        meta['usage'] = event['usage']
                    if event.get('error'):
                        raise LLMHTTPError(
                            f'流式响应返回错误: {self._short(event.get("error"), 200)}',
                            retryable=False,
                        )
                    piece = self._extract_stream_piece(event)
                    if piece:
                        if meta['ttft'] is None:
                            meta['ttft'] = time.time() - started
                        pieces.append(piece)
                        meta['chunks'] += 1
                    else:
                        reasoning_piece = self._extract_reasoning_piece(event)
                        if reasoning_piece:
                            reasoning_parts.append(reasoning_piece)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                text = ''.join(pieces)
                if text.strip():
                    # 上游已经生成并很可能已经计费，直接采用已收到的内容
                    meta['partial'] = True
                    meta['reason'] = type(exc).__name__
                    logger.warning(
                        f'[LLM] partial#{request_id} 读取中断({type(exc).__name__})，'
                        f'已收到 {len(text)} 字符，直接采用已收到内容'
                    )
                    return text, meta
                raise
            if not pieces and reasoning_parts:
                # 推理模型（deepseek-reasoner / Qwen3-thinking 等）有时正文为空，
                # 结论全在思考内容里；不兜底就等于整题白问一次。
                text = ''.join(reasoning_parts)
                if text.strip():
                    meta['reasoning_only'] = True
                    meta['chunks'] = 1
                    logger.warning(
                        f'[LLM] reasoning-only#{request_id} 正文为空，'
                        f'改用思考内容（{len(text)} 字符）解析答案'
                    )
                    return text, meta
            if not pieces and raw_lines:
                # 服务端既没给 Content-Type 也没按 SSE 输出时，最后再试一次整体解析
                try:
                    text, meta_full = self._read_full_body(response, request_id, meta,
                                                           raw_lines=raw_lines)
                except LLMError:
                    text, meta_full = '', meta
                if text:
                    return text, meta_full
            return ''.join(pieces), meta
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover
                pass

    def _read_full_body(self, response: requests.Response, request_id: str,
                        meta: Dict[str, Any],
                        raw_lines: Optional[List[bytes]] = None) -> Tuple[str, Dict[str, Any]]:
        """把整段响应体当普通 JSON 解析。

        用于服务端忽略了 `"stream": true`、直接返回一个完整 JSON 的情况。
        """
        body = b'\n'.join(raw_lines) if raw_lines is not None else response.content
        try:
            event = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LLMResponseError(
                '服务端既没有返回流式事件，也不是合法 JSON: '
                f'{self._short(body.decode("utf-8", "replace"), 200)}'
            )
        if not isinstance(event, dict):
            raise LLMResponseError(f'响应结构异常: {self._short(event, 200)}')
        if event.get('error'):
            raise LLMHTTPError(
                f'响应返回错误: {self._short(event.get("error"), 200)}', retryable=False
            )
        meta['provider_id'] = event.get('id') or meta.get('provider_id')
        meta['usage'] = event.get('usage') or event.get('usageMetadata') or meta.get('usage')
        meta['chunks'] = 1
        logger.info(f'[LLM] full-body#{request_id} 已按整体 JSON 解析（服务端未流式输出）')
        return self._extract_full_text(event), meta

    def _ask_once(self, payload: Dict[str, Any], headers: Dict[str, str], request_id: str,
                  read_timeout: float, started: float) -> Tuple[str, Dict[str, Any]]:
        meta: Dict[str, Any] = {
            'status': None, 'ttft': None, 'usage': None, 'provider_id': None,
            'partial': False, 'chunks': 1, 'reason': '',
        }
        response = self._post(payload, headers, read_timeout)
        meta['status'] = response.status_code
        try:
            self._raise_for_status(response, request_id)
            body = response.content
            try:
                event = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise LLMResponseError(
                    f'响应不是合法 JSON: {self._short(body.decode("utf-8", "replace"), 200)}'
                )
            if not isinstance(event, dict):
                raise LLMResponseError(f'响应结构异常: {self._short(event, 200)}')
            if event.get('error'):
                raise LLMHTTPError(
                    f'响应返回错误: {self._short(event.get("error"), 200)}', retryable=False
                )
            meta['provider_id'] = event.get('id')
            meta['usage'] = event.get('usage') or event.get('usageMetadata')
            meta['ttft'] = time.time() - started
            return self._extract_full_text(event), meta
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover
                pass

    # ---------- 对外接口 ----------

    def ask(self, question: str, timeout: Optional[float] = None) -> str:
        """向模型提问并返回文本回答。

        Args:
            question: 题目与选项文本
            timeout: 覆盖配置里的读取超时（秒）
        """
        payload = self._build_payload(question)
        request_id = uuid.uuid4().hex[:10]
        read_timeout = float(timeout or self.timeout_override or self.settings.read_timeout)
        attempts = max(0, int(self.settings.max_retries)) + 1
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            headers = self._build_headers(request_id)
            started = time.time()
            logger.info(
                f'[LLM] req#{request_id} attempt={attempt}/{attempts} '
                f'provider={self.settings.provider} model={self.settings.model} '
                f'stream={self.settings.stream} read_timeout={read_timeout:g}s url={self.url}'
            )
            try:
                if self.settings.stream:
                    text, meta = self._ask_stream(payload, headers, request_id, read_timeout, started)
                else:
                    text, meta = self._ask_once(payload, headers, request_id, read_timeout, started)
            except requests.exceptions.SSLError as exc:
                raise LLMError(f'TLS 握手失败: {self._short(exc)}') from exc
            except requests.exceptions.Timeout as exc:
                last_error = LLMTimeoutError(
                    f'读取超时（{read_timeout:g}s 内没有收到新数据）: {self._short(exc)}'
                )
                logger.warning(
                    f'[LLM] timeout#{request_id} attempt={attempt}/{attempts} '
                    f'elapsed={time.time() - started:.2f}s detail={self._short(exc)}'
                )
            except requests.exceptions.ConnectionError as exc:
                # requests 会把流式读取超时包成 ConnectionError，这里区分开，
                # 否则日志里"读超时"会被误报成"连不上"，排查方向完全相反。
                if _looks_like_read_timeout(exc):
                    last_error = LLMTimeoutError(
                        f'读取超时（{read_timeout:g}s 内没有收到新数据）: {self._short(exc)}'
                    )
                    logger.warning(
                        f'[LLM] timeout#{request_id} attempt={attempt}/{attempts} '
                        f'kind=read(stream) elapsed={time.time() - started:.2f}s '
                        f'detail={self._short(exc)}'
                    )
                else:
                    last_error = LLMError(f'连接失败: {self._short(exc)}')
                    logger.warning(
                        f'[LLM] conn-error#{request_id} attempt={attempt}/{attempts} '
                        f'elapsed={time.time() - started:.2f}s detail={self._short(exc)}'
                    )
            except LLMHTTPError as exc:
                if not exc.retryable or attempt >= attempts:
                    raise
                last_error = exc
                logger.warning(
                    f'[LLM] http-retry#{request_id} attempt={attempt}/{attempts} '
                    f'status={exc.status} elapsed={time.time() - started:.2f}s'
                )
            else:
                elapsed = time.time() - started
                ttft = meta.get('ttft')
                ttft_text = f'{ttft:.2f}s' if isinstance(ttft, (int, float)) else 'n/a'
                logger.info(
                    f'[LLM] resp#{request_id} status={meta.get("status")} '
                    f'elapsed={elapsed:.2f}s ttft={ttft_text} chars={len(text)} '
                    f'chunks={meta.get("chunks")} partial={meta.get("partial")} '
                    f'provider_id={meta.get("provider_id")} usage={meta.get("usage")}'
                )
                return text

            if attempt < attempts:
                wait = min(2 ** (attempt - 1), 8)
                logger.info(f'[LLM] backoff#{request_id} 等待 {wait}s 后重试')
                time.sleep(wait)

        raise last_error or LLMError('LLM 调用失败')


# ==================== 工厂 ====================


def build_settings(provider: Any, **overrides: Any) -> LLMSettings:
    """从本地配置构造 LLMSettings（overrides 优先）。"""
    name = normalize_provider(provider)
    preset = PRESETS.get(name, PRESETS['deepseek'])
    stored = load_model_config(name) or {}
    options = load_llm_options(name) or {}
    default_base = MODEL_CONFIGS.get(name, {}).get('base_url', '')

    values: Dict[str, Any] = {
        'provider': name,
        'base_url': (stored.get('base_url') or default_base or '').strip(),
        'model': (stored.get('model') or MODEL_CONFIGS.get(name, {}).get('model', '') or '').strip(),
        'api_key': (stored.get('api_key') or '').strip(),
        'dialect': preset.get('dialect', 'openai'),
        'connect_timeout': options.get('connect_timeout', 10.0),
        'read_timeout': options.get('read_timeout', 180.0),
        'stream': options.get('stream', True),
        'max_retries': options.get('max_retries', 2),
        'temperature': options.get('temperature'),
        'extra_body': options.get('extra_body') or {},
    }
    values.update({k: v for k, v in overrides.items() if v is not None})
    return LLMSettings(**values)


def create_client(provider: Any, session: Optional[requests.Session] = None,
                  **overrides: Any) -> LLMClient:
    """按 provider 构造客户端（'1'/'2'/'3' 与 provider 名都接受）。"""
    return LLMClient(build_settings(provider, **overrides), session=session)
