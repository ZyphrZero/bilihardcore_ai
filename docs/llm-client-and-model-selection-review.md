# LLM 请求层 / 模型选择模块 —— 代码评审与改进建议

> 评审对象：`bilihardcore_ai`（当前工作区 HEAD `b1784e8` + 未提交改动）
> 症状：正常 API 请求，模型提供商后台显示「成功 / 已计费」，本地日志却是
> `Read timed out. (read timeout=30)`，然后整场答题直接结束。
> 结论：**不是网络问题，是客户端请求模型设计问题**——非流式 + 30s 读超时 +
> 无重试 + 单次异常终止整个会话。模型选择/配置层则存在「多份真值、选择不持久化、
> 校验校验的是输入框而不是落盘配置」等结构性问题。

---

## 1. 证据链（日志直接定位）

`logs/2026-11-15.log` 第 17–22 行：

```
23:29:43  第1题:中国书法的"亚圣"是谁 … 1.颜真卿 2.孟轲 3.田英章 4.王献之
23:30:23  ERROR 答题过程发生错误: 自定义模型API请求失败:
          HTTPSConnectionPool(host='www.yyapi.cloud', port=443): Read timed out. (read timeout=30)
```

第 27–32 行是同一个问题的复现：

```
23:44:00  第1题:中国书法的"亚圣"是谁 …
23:44:31  ERROR … Read timed out. (read timeout=30)
```

三条即时可得的结论：

1. **是 ReadTimeout 而不是 ConnectTimeout** → DNS / TLS / 代理都是通的，
   请求确实到达了服务端，卡在「等响应体」。
2. **两次都卡在 30s 附近**（40s / 31s）→ 命中 `tools/LLM/custom.py:92` 里写死的
   `timeout=30`。
3. **同一道题被重复请求了两次**（23:29:43 与 23:44:00）→ 上游两次都算成功、
   两次计费，而本地两次都拿不到结果。

这与「提供商后台成功、本地日志超时」完全吻合：**客户端主动放弃，服务端继续生成并落账单**。

> 另一个细节：日志里所有条目的来源都是 `gui.threads:patched_info:150`，
> 说明 `gui/threads.py:146-176` 对 loguru 的 monkey-patch 把原始调用点
> （`scripts/start_senior:start:53` 等）全部抹掉了 —— 这也是排障困难的元凶之一，见 §5。

---

## 2. 为什么会「后台成功、本地超时」

### 2.1 `timeout=30` 的语义被误用

`requests` 的 `timeout` 是 **连接超时 + 相邻两次收到字节的间隔超时**，不是总时长。

- 流式响应：每收到一个 chunk 就重置计时器，30s 完全够用。
- **非流式响应**：从「发完请求」到「整段 JSON 到齐」之间可能一个字节都没有，
  于是 30s 实际等于「整段生成时间必须 ≤ 30s」。

当前 4 个 provider 全部是 `stream=False`（默认），所以读超时就是
「模型总耗时的上限」。一旦上游是推理模型 / 高峰期排队 / 中转站做了缓冲转发，
必然超时。

### 2.2 中转站（`www.yyapi.cloud`）进一步放大该问题

绝大多数 OpenAI 兼容中转站的工作方式是：客户端请求进来 → 中转站**非流式**地
调上游 → 上游生成完毕 → 中转站一次性返回 JSON。也就是说中转站内部是「攒完再发」，
客户端这边 30s 内一个字节都收不到是常态。

即使中转站支持 `stream: true`，客户端也不请求流式，等于自己把「渐进式到达」
这个唯一能续命读超时的机制关掉了。

### 2.3 自己加的 `thinking` / `temperature` 不是解药

未提交的改动给 `tools/LLM/custom.py:84-85` 加了：

```python
"thinking": {"type": "disabled"},
"temperature": 0
```

- `thinking` 不是 OpenAI 标准字段；不同中转站的处理是：**原样透传 / 直接 400 / 静默忽略**。
  yyapi.cloud 上大概率属于「透传后被上游忽略」，所以「思考模型提速」的目的没达到，
  反而让请求更容易被判成非法参数（一旦换一家中转站就会 400）。
- 真正决定「会不会 30s 超时」的是 §2.1 的流式/超时设计，和 `thinking` 无关。
  这解释了「加了参数还是超时」。

### 2.4 超时的后果被放大成「整场结束」

`scripts/start_senior.py:53` 的 `llm.ask(...)` 抛异常 → 直接落到 `:82-83` 的
外层 `except Exception` → `logger.error("答题过程发生错误")` → 会话 return。

也就是说：**第 1 题一次 30s 超时 = 这一轮 100 题全部作废**，用户只能重新点开始，
而重开时第 1 题会被再请求一次（日志中已发生），上游再次计费。

---

## 3. 请求层问题清单

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| 3.1 | `custom.py:88-93` `deepseek.py:33-38` `gemini.py:38-44` `openai.py:34-39` | 超时写死且不可配置；`openai.py:15` 甚至只有 **10s**；未区分 connect / read | 非流式场景下等于「生成时间上限」，慢模型必挂 |
| 3.2 | 4 个 provider | 全部非流式，不支持 SSE | 读超时无法续命；用户全程无进度反馈；无法感知「已生成一半」 |
| 3.3 | 全部 | **无重试、无退避、无熔断** | 一次抖动 = 整场结束（叠加 2.4） |
| 3.4 | 全部 | 每次 `requests.post` 新建连接，无 `Session` 连接池 | 100 题 = 100 次 TLS 握手；代理场景下额外 1–3s/次，正是压在超时边缘的那部分 |
| 3.5 | `custom.py:96-97`、其余 `except` | 错误信息只有 `str(e)`：没有 URL、model、HTTP 状态码、耗时、响应体、response id | 用户无法和提供商后台对账，只能看到「time out」 |
| 3.6 | `deepseek.py:40` `gemini.py:46` `openai.py:41` | `response.json()["choices"][0]...` 裸取，无 KeyError 防护 | 429 / 余额不足 / HTML 错误页 → 抛 KeyError，提示文不对题 |
| 3.7 | `custom.py:23-28` | URL 归一化靠「结尾是 `/` 还是 `#`」这种隐式约定；无法表达「我填的就是完整 endpoint」 | 用户填 `.../v1/chat/completions` → 变成 `.../chat/completions/chat/completions` → 404 |
| 3.8 | `custom.py:55-59` | 用 `'dashscope' in base_url` 猜协议 → `f"{base}/compatible-mode/v1/chat/completions"` | 域名/路径里带 `aliyuncs` 的中转站会被误判；用户若已填兼容模式地址 → 双后缀 404 |
| 3.9 | `gemini.py:15` vs `widgets.py:302` | 代码拼 `/models/{m}:generateContent`，但 UI 占位符示例是 `https://generativelanguage.googleapis.com`（缺 `/v1beta`） | 照占位符填 → 404 |
| 3.10 | `custom.py:41-44, 67-69` | `print` 调试（打包 `console=False` 后是静默 no-op） | 用户永远看不到「实际请求的 URL / model」 |
| 3.11 | `tools/request_b.py:62, 90` | B 站请求**完全没设 timeout** | 网络异常时线程永久挂起，用户以为「卡死/超时」，只能强杀 |
| 3.12 | `custom.py:53` 等 | 类型注解 `-> Dict[str, Any]` 实为 `str` | 静态检查失效，调用方容易误用 |
| 3.13 | `start_senior.py:39-46` | 每题都 `new` 一个 provider 对象（重读配置、重打日志） | 100 题 100 次磁盘读；配置在答题中变更会造成中途换模型 |

---

## 4. 模型选择 / 配置层问题清单（「选择部分不好用」的根因）

### 4.1 「当前模型」有 5 份真值

| 位置 | 形态 | 备注 |
|------|------|------|
| `config/config.py:142` | 模块级全局 `model_choice = '1'` | 进程内可变，无持久化 |
| `core/model_manager.py:48` | `self.current_model = 'deepseek'` | GUI 从不调用 `set_current_model`，实际上从未生效 |
| `gui/widgets.py:388` | `self.current_model_type = "deepseek"` | 下拉框初值，每次启动重置 |
| `scripts/start_senior.py:9` | `from config.config import model_choice` | **导入期快照**；`main_window.py:213` 改 `config.config.model_choice` 改不到它 |
| `scripts/start_senior.py:20` | `self.current_model = model_choice` | 构造期拷贝，之后靠 `update_model_choice` 手动纠正 |

`main_window.py:212-214` 那一行 `import config.config; config.config.model_choice = ...`
其实**没有效果**，只是被紧接着的 `update_model_choice()` 掩盖了。任何新增调用路径
（比如 `AppController.start_quiz()` 之外的入口）都会静默用回 DeepSeek。

### 4.2 选择不持久化

`SettingsWidget` 只保存 `base_url / model / api_key`，**不保存「用户选了哪一家」**。
下次启动下拉框回到 DeepSeek；用户忘了再选一次 → 用 DeepSeek 的空 key 开始 →
报「API密钥缺失」或「DeepSeek API request failed: 401」。

### 4.3 校验的是输入框，请求读的是磁盘

- `widgets.py:448-467` `get_current_model_info()` 返回的 `api_key` 来自 `QLineEdit`；
- `main_window.py:207` 只用它判断「非空」；
- 真正发请求时 `custom.py:38` 用 `load_api_key('custom')` 从磁盘读。

于是「输入了 key 但没点保存」→ 校验通过 → 运行时报
`自定义模型的API密钥为空`。反过来「保存了但输入框被改后未保存」→ 用的是旧 key。
这类「校验与执行不同源」是「明明配置了却报错」类工单的主要来源。

### 4.4 保存是非原子的两次写

`widgets.py:363-367`：先 `save_api_key()`（内部 `load` + 整文件 `write`），
再 `save_model_config()`（再 `load` + 再 `write`）。中途崩溃/杀进程 → 半生效：
key 是新的、URL 是旧的。`config.py:74-93` 用的是 `open(..., 'w')` 直接截断写，
没有 tmp + `os.replace`，也没有锁。

### 4.5 两套默认值互相矛盾

- `core/model_manager.py:24`：`base_url = 'https://api.deepseek.com'`
- `config/config.py:11`：`base_url = 'https://api.deepseek.com/v1'`

两套注册表（`AVAILABLE_MODELS` 与 `MODEL_CONFIGS` + `MODEL_DISPLAY_INFO`）并存，
`choice_value` 也在两处重复定义。改一处漏一处，迟早不一致。

### 4.6 保存失败是静默的

`model_manager.py:84-96`：

```python
if base_url and model_name:
    save_model_config(...)
```

任一为空就**什么都不做也不报错**。UI 上表现为「点了保存没反应」。

### 4.7 没有「测试连接」/「拉取模型列表」

用户唯一的验证手段是「跑满 100 题」或者「看日志里那句 time out」。
`GET /v1/models` 这种零成本自检完全没有，导致「模型名写错」也要等到答题时才暴露。

### 4.8 key 明文落盘

`~/.bili-hardcore/<provider>_config.json` 明文，权限未收紧，也没有 keyring 选项。

---

## 5. 日志 / 线程层

1. `gui/threads.py:146-176` 运行时替换 `logger.info/error/warning`：
   - **丢失原始模块/行号**（日志已自证：全是 `gui.threads:patched_info:150`）；
   - `patched_info(msg, *args)` 直接 `f"INFO: {msg}"`，loguru 的 `{}` 占位参数不会展开；
   - 全局副作用：任何线程、任何库的日志都被改写；`_patch_logger` 与 `_restore_logger`
     在多线程下有竞态（保存 `orig_*` 只做一次）。
   → 正确做法：`logger.add(sink)` 挂一个 Qt 信号桥（见 §7.4），完全不用改 loguru 的方法。
2. `gui/threads.py:186` `self.terminate()`：硬杀线程。若此刻正在 `requests.post`，
   连接直接废掉，而上游仍在生成并计费 —— 又一个「后台成功、本地什么都没有」的来源。
3. `gui/threads.py:74, 86` `captcha_wait_event.wait(30)`：用户 30s 内没填验证码就返回 `""`，
   随后 `captcha_submit` 必然失败。没有「超时提示」，用户只看到「验证过程发生错误」。
4. `start_senior.py:30-59` 里 4 段「检查是否停止」样板，噪音大、易漏。

---

## 6. 建议的目标架构

```
┌──────────── GUI（设置页）
│  预设下拉（DeepSeek / Gemini / 百炼 / 自定义中转）
│  └─ 高级：endpoint 覆盖、connect/read 超时、stream 开关、额外 body(JSON)、代理
│  [测试连接]  [拉取模型列表]   ← 显示：有效 URL / HTTP 状态 / TTFT / 总耗时 / 返回片段
│
├──────────── SettingsStore（单一真值 + 原子写 + schema 版本 + 迁移）
│  ~/.bili-hardcore/settings.json
│  { "version": 2, "active_provider": "custom",
│    "providers": { "custom": { "base_url": ..., "model": ..., "api_key": ...,
│                              "timeout": {"connect": 10, "read": 120},
│                              "stream": true, "max_retries": 2,
│                              "extra_body": {"thinking": {"type": "disabled"}} } } }
│
└──────────── LLMClient（一个类，替代 4 个 provider 类）
   预设表 PRESETS + 统一 OpenAI Chat Completions 协议
   ├ 流式 SSE（默认开）→ 逐 chunk 累加，读超时按 chunk 续命
   ├ 超时 (connect, read) 可配
   ├ 重试：仅对 ConnectTimeout / ReadTimeout / 5xx / 429 做指数退避（1s,2s,4s）
   ├ 每请求生成 request_id，结构化日志 + 用量统计
   └ 答案解析：正则提取 1..N，失败带上原文重试（不重新取题）
```

关键判断：**4 个 provider 可以合并成 1 个客户端**。DeepSeek、Gemini、DashScope
现在都提供 OpenAI 兼容端点：

| 提供商 | OpenAI 兼容 base |
|--------|------------------|
| DeepSeek | `https://api.deepseek.com`（`/v1` 亦可） |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 自定义/中转 | 用户填，默认补 `/v1` |

Gemini 走 OpenAI 兼容端点后，`gemini.py`（原生 `contents/parts` 格式）和它的
`?key=` 鉴权分支可以整体删掉，URL 拼接的坑也随之消失。

---

## 7. 关键代码骨架

### 7.1 统一客户端（含流式 + 重试）

```python
# tools/LLM/client.py（新）
import json, time, uuid, re
import requests
from dataclasses import dataclass, field
from typing import Iterator

RETRYABLE = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
)

@dataclass
class LLMSettings:
    base_url: str
    model: str
    api_key: str
    connect_timeout: float = 10.0
    read_timeout: float = 120.0      # 非流式时的兜底；流式时按 chunk 续命
    stream: bool = True
    max_retries: int = 2
    extra_body: dict = field(default_factory=dict)

class LLMClient:
    def __init__(self, settings: LLMSettings):
        self.s = settings
        self.session = requests.Session()          # 连接池复用
        self.url = self._resolve_endpoint(settings.base_url)

    @staticmethod
    def _resolve_endpoint(base: str) -> str:
        b = (base or "").strip().rstrip("/")
        if b.endswith("/chat/completions"):
            return b                            # 用户直接给了完整 endpoint
        if b.endswith("/v1"):
            return f"{b}/chat/completions"
        return f"{b}/v1/chat/completions"

    def ask(self, question: str, prompt_tpl: str) -> str:
        rid = uuid.uuid4().hex[:8]
        payload = {
            "model": self.s.model,
            "messages": [{"role": "user",
                          "content": prompt_tpl.format(time.time(), question)}],
            "stream": self.s.stream,
            **self.s.extra_body,                 # thinking / temperature 交给用户配置
        }
        headers = {"Authorization": f"Bearer {self.s.api_key}",
                   "Content-Type": "application/json",
                   "X-Request-Id": rid}

        last = None
        for attempt in range(1, self.s.max_retries + 2):
            t0 = time.time()
            try:
                if self.s.stream:
                    return self._stream(payload, headers, rid, attempt)
                return self._once(payload, headers, rid, attempt, t0)
            except RETRYABLE as e:
                last = e
                logger.warning(f"LLM req#{rid} attempt={attempt} 超时/连接失败: {type(e).__name__} "
                               f"elapsed={time.time()-t0:.1f}s url={self.url}")
                if attempt > self.s.max_retries:
                    break
                time.sleep(min(2 ** attempt, 8))
        raise LLMTimeoutError(f"req#{rid} 已重试 {self.s.max_retries} 次仍失败: {last}")

    def _once(self, payload, headers, rid, attempt, t0):
        r = self.session.post(self.url, json=payload, headers=headers,
                              timeout=(self.s.connect_timeout, self.s.read_timeout))
        self._check(r, rid)
        data = r.json()
        logger.info(f"LLM resp#{rid} status={r.status_code} "
                    f"elapsed={time.time()-t0:.2f}s usage={data.get('usage')}")
        return data["choices"][0]["message"]["content"]

    def _stream(self, payload, headers, rid, attempt) -> str:
        r = self.session.post(self.url, json=payload, headers=headers, stream=True,
                              timeout=(self.s.connect_timeout, self.s.read_timeout))
        self._check(r, rid)
        buf, ttft, t0 = [], None, time.time()
        for raw in r.iter_lines(chunk_size=256, decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            body = raw[5:].strip()
            if body == "[DONE]":
                break
            try:
                evt = json.loads(body)
            except json.JSONDecodeError:
                continue
            delta = (evt.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta:
                ttft = ttft or time.time() - t0
                buf.append(delta)
        text = "".join(buf)
        logger.info(f"LLM resp#{rid} stream ttft={ttft} total={time.time()-t0:.2f}s chars={len(text)}")
        return text

    @staticmethod
    def _check(r, rid):
        if r.status_code >= 400:
            body = r.text[:500]
            raise LLMHTTPError(f"req#{rid} HTTP {r.status_code}: {body}")
```

要点：
- **`stream=True` 是消除「后台成功/本地超时」的主要手段**：只要上游在吐字，
  读超时就不会触发；120s 的 read 只用于「彻底静默」的兜底。
- 只有 `ConnectTimeout/ReadTimeout/ConnectionError` 才重试；HTTP 4xx 不重试
  （避免把「余额不足」重试成一串账单）。
- 每次尝试都打 `url / model / attempt / elapsed`，用户可以直接和后台对账。

### 7.2 答案解析（替代 `int(answer)`）

```python
ANS_RE = re.compile(r"(?<!\d)([1-9])(?!\d)")

def parse_choice(text: str, max_choice: int) -> int | None:
    t = (text or "").strip()
    m = ANS_RE.search(t)
    if m and 1 <= int(m.group(1)) <= max_choice:
        return int(m.group(1))
    return None          # 交给调用方「同一题重问」而不是「重新取题」
```

配合 `start_senior.py:61-68`：解析失败 → 同题重问（最多 3 次，附「只输出序号」的
强约束提示），**不要 `continue` 重新 `get_question()`**——那样会白耗一题并再次计费。

### 7.3 配置原子写

```python
def save_settings(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = os.path.join(CONFIG_DIR, "settings.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(CONFIG_DIR, "settings.json"))   # 原子
    try:
        os.chmod(os.path.join(CONFIG_DIR, "settings.json"), 0o600)
    except OSError:
        pass
```

一次性写入 `active_provider + 各 provider 配置`，杜绝 §4.4 的半生效。

### 7.4 日志 sink（替代 monkey-patch）

```python
# gui/log_bridge.py
from PySide6.QtCore import QObject, Signal

class LogBridge(QObject):
    message = Signal(str)
    def write(self, text: str) -> None:
        self.message.emit(text.rstrip())

bridge = LogBridge()
bridge.message.connect(log_widget.append_log)          # Qt::QueuedConnection，线程安全
logger.add(bridge, level="INFO",
           format="{time:HH:mm:ss} | {level: <7} | {name}:{function}:{line} - {message}")
```

好处：保留真实调用点；`{}` 占位参数正常展开；不需要 `_patch_logger/_restore_logger`；
不会被 `terminate()` 打断。

### 7.5 排障脚本（可直接跑，先定位再改代码）

```python
import json, time, requests

BASE = "https://www.yyapi.cloud"
KEY  = "sk-..."                      # 你自己的
MODEL = "gpt-4o-mini"                # 换成后台显示成功的那个

s = requests.Session()
print("models:", s.get(f"{BASE}/v1/models",
                       headers={"Authorization": f"Bearer {KEY}"}, timeout=15).status_code)

payload = {"model": MODEL, "stream": True,
           "messages": [{"role": "user", "content": "1+1=? 只回答数字"}]}
t0 = time.time()
r = s.post(f"{BASE}/v1/chat/completions", json=payload,
           headers={"Authorization": f"Bearer {KEY}"}, stream=True, timeout=(10, 120))
print("status:", r.status_code, r.headers.get("content-type"))
first = None
for line in r.iter_lines(decode_unicode=True):
    if line and line.startswith("data:") and first is None:
        first = time.time() - t0
        print(f"TTFT = {first:.2f}s  first_chunk = {line[:120]}")
print(f"total = {time.time()-t0:.2f}s")
```

判读：
- `content-type: text/event-stream` 且有 TTFT → 中转站支持流式 → §7.1 直接解决问题；
- `content-type: application/json` 且 total ≈ 30–60s → 中转站不支持流式，只能靠
  放大 read 超时（`read_timeout=180`）并给它单独配「非流式」分支；
- `status=404` → 是 URL 归一化问题（§3.7/3.8），不是超时问题。

---

## 8. 优先级路线图

### P0（半天内可完成，直接解决「后台成功/本地超时」）

| 项 | 改动点 | 验收 |
|----|--------|------|
| 超时可配 + 区分 connect/read | `custom.py:92` 等 4 处 → `timeout=(10, 180)`，从设置读 | 慢模型不再 30s 断 |
| 开流式 | `payload["stream"]=True` + `iter_lines` 解析 SSE | 日志出现 `ttft=… total=…`，无 ReadTimeout |
| 单题失败不终止整场 | `start_senior.py:53` 包 try/except，同题重试 2 次后再取新题 | 一次抖动不影响 100 题 |
| 结构化请求日志 | 打 `req#id url model attempt elapsed status usage` | 能和提供商后台逐条对上 |
| 删掉 `print` 调试 | `custom.py:41-44,67-69` → `logger.debug` | 打包版也能看到 URL |
| 答案解析用正则 | `start_senior.py:61-68` | 「答案：3」「3.」「３」都能正确解析 |

### P1（1–2 天）

1. 合并为单一 `LLMClient` + `PRESETS`（删 `deepseek/gemini/openai/custom` 四份重复）。
2. `SettingsStore`：单一 JSON、原子写、`active_provider` 持久化、schema 版本 + 迁移。
3. UI：预设下拉 + 高级折叠（endpoint 覆盖 / 超时 / stream / extra_body / 代理）
   +「测试连接」「拉取模型列表」+ 显示有效 URL、状态码、TTFT。
4. 校验与执行同源：`start_quiz` 前强制 `save()` 并回读，用回读结果发请求。
5. 修 URL 归一化：完整 endpoint / `/v1` / 裸域名三种输入都正确（见 §7.1）。
6. B 站请求补 `timeout=15`（`request_b.py:62,90`）。
7. 日志改 sink，删除 monkey-patch；`stop()` 改协作式取消（`Event`），不再 `terminate()`。

### P2（体验/健壮性）

1. key 入 keyring，配置文件 0600。
2. 用量与成本统计（按 provider/model 累计 token），答题页显示。
3. 每 provider 的最小请求间隔（README 说 Gemini 要 5s，代码只 `sleep(1)`）。
4. 失败题记录 → 「断点续答」真正可用。
5. 单元测试：`responses`/`respx` 模拟 SSE 分块、模拟 30s 静默、模拟 429；
   加一条「超时后仍返回完整内容」的回归用例。
6. CI 加 `ruff` + `mypy`（能直接抓出 §3.12 的注解错误）。

---

## 9. 五个「一行就能改」的顺手修复

1. `tools/LLM/openai.py:15`：`timeout: Optional[int] = 10` → `60`（其余三个是 30，它特立独行）。
2. `tools/LLM/deepseek.py:7-12`：`__init__` 补 `if not self.api_key: raise ValueError(...)`，
   否则会发出 `Authorization: Bearer None` 然后得到一个语焉不详的 401。
3. `gui/widgets.py:302`：Gemini 占位符补 `/v1beta`（或按 §7.1 归一化）。
4. `core/model_manager.py:94`：`if base_url and model_name` 去掉条件，改为
   「缺字段 → 抛错」而不是静默跳过。
5. `config/config.py:142`：删除 `model_choice` 全局，改由 `QuizSession` 构造参数注入。

---

## 10. 一句话总结

> 现在的实现把「一次 LLM 调用」当成「一次快速的同步 HTTP 请求」来写：
> 写死 30s、不支持流式、不重试、异常直接终止会话、错误信息不带任何上下文；
> 而模型选择层又有 5 份真值、不持久化、校验和落盘不同源。
> 修好 **流式 + 可配超时 + 单题容错 + 结构化日志**（P0，约半天），
> 「后台成功、本地超时」这类现象就会消失；再合并成单一客户端（P1），
> 「选择部分不好用」的绝大部分工单也会一起消失。

---

## 11. P0 已实施（本仓库现状）

> 本节记录**已经落到代码里**的部分，与第 5~8 节的"建议代码"可能略有差异，
> 以本节为准。

### 11.1 新增/改动的文件

| 文件 | 变化 |
| --- | --- |
| `tools/LLM/client.py` | **新增**。`LLMSettings` / `LLMClient` / `resolve_endpoint()` / `parse_choice()` / `create_client()`，四个 provider 共用的唯一实现 |
| `tools/LLM/{deepseek,gemini,custom,openai}.py` | 改为薄包装（保留原类名与 `ask()` 签名），实际逻辑全部委托给 `client.py` |
| `config/config.py` | 新增 `load_llm_options()`；`save_model_config(..., options=)` 改为**合并 + 原子写**（tmp + `os.replace`） |
| `scripts/start_senior.py` | `QuizSession` 支持依赖注入；LLM 失败**不再终止整场**；答案解析改用正则；去掉 `model_choice` 导入期快照 |
| `gui/widgets.py` | 模型设置页新增「连接超时 / 读取超时 / 失败重试 / 流式 / 额外参数(JSON)」；保存改为一次原子写 |
| `core/model_manager.py` | 去掉 `if base_url and model_name` 的静默跳过，缺字段直接抛错 |
| `tests/` | 新增 `fake_llm_server.py`、`test_llm_client.py`、`test_quiz_loop.py`、`test_config_store.py`（纯 stdlib unittest，只打 127.0.0.1） |

### 11.2 行为变化（升级后用户可感知）

1. **默认开启流式**（`stream=True`）。这是"后台成功、本地超时"的直接解法：
   `read_timeout` 从"整段生成上限"变成"相邻两次收到数据的最长间隔"。
2. **读取超时默认 180s**（原来是写死的 30s），连接超时 10s，且都能在设置页改。
   注意：在旧实现里 30s 是**整体上限**，所以调大也救不了慢中转站；现在语义变了，
   180s 是很保守的值，一般不需要再动。
3. **失败重试默认 2 次**，只对「超时 / 连接中断 / 408 / 409 / 425 / 429 / 5xx」重试；
   4xx（如 401 密钥错误）不重试，避免把配额烧在必然失败的请求上。
4. **流式读到一半就断时，直接返回已收到的内容**（日志标 `partial=True`）。
   以前这种情况下会抛异常 → 整场会话结束 → 重开又把同一题重发一次，白花一次钱。
5. **单题失败不再毁掉整场**：取答案失败连续 3 次才停（并提示检查网络/代理或调大读取超时）；
   模型答非所问（解析不出选项）最多重问 3 次，仍不行就跳过该题继续下一题。
6. **`thinking` / `temperature` 不再默认发送**。之前 `custom.py` 里硬编码了
   `{"thinking": {"type": "disabled"}}` 与 `temperature: 0`，这两个字段不是 OpenAI 标准字段，
   严格网关会直接 400。现在改成设置页「额外参数(JSON)」里按需填写，默认不发。
7. **日志可直接指向问题**：每次请求一行 `[LLM] req#<id> … url=<解析后的真实地址>`，
   返回一行 `[LLM] resp#<id> status=… elapsed=… ttft=… chars=… usage=…`，
   端到端共享同一个 `X-Request-Id`，可与提供商后台的 request id 对照。
8. **网关忽略 `stream: true` 时自动降级**：部分中转站会无视流式参数、直接返回完整 JSON。
   客户端识别到 `Content-Type` 不是 `event-stream` 时会整体解析（日志 `[LLM] full-body#…`），
   不会再把"有答案但没按 SSE 返回"误判成模型答非所问。

### 11.3 怎么跑测试

```bash
python -m unittest discover -s tests -t .
# Ran 49 tests ... OK
```

测试只监听 `127.0.0.1` 上的假 LLM 服务，**不访问外网**；
`test_quiz_loop.py` 还把 `client.senior.get/post`、`tools.request_b.get/post` 全部
替换成 `AssertionError`，**任何真实 B 站请求都会让测试直接失败**。
`test_gui_presets.py` 用 `QT_QPA_PLATFORM=offscreen` 离屏构造设置页控件，只验证
「思维链」预设的合并逻辑，不开窗口、不发请求。

### 11.4 仍然遗留（P1/P2）

- 「当前模型」仍有多份真值（`config.model_choice`、`ModelManager.current_model`、
  `ModelConfigWidget.current_model_type`），选择结果仍不持久化，重启回默认。
- `get_current_model_info()` 仍读控件而不是读已落盘的配置（"校验与执行不同源"）。
- `gui/widgets.py` 仍要求 API Key 长度 ≥ 10、三个字段全填，自建推理服务
  （vLLM / LM Studio，key 随便填）会被挡住。
- `gui/threads.py` 的 loguru monkey-patch、`self.terminate()` 硬杀线程、
  验证码 `wait(30)` 静默失败都还在。
- `tools/request_b.py` 的 B 站请求仍无 timeout（P1，不影响 LLM 侧）。

## 12. 让模型「不返回思维链、只输出答案」

思维链既费 token 又拖慢首字，更危险的是**正文里夹带推理过程会让答案解析取到错误的数字**
（模型罗列「1. 甲 2. 乙 3. 丙」时，取第一个数字几乎必然得到 1）。三层手段，按有效性排序：

### 12.1 提供商参数（最有效，能真正省掉推理开销）

这些都不是 OpenAI 标准字段，属于各家扩展，所以做成了设置页里**可选的下拉预设**，
只在你主动选择时才写进请求体（写错字段的严格网关会直接 400，所以不能默认发）：

| 场景 | 参数 |
| --- | --- |
| 阿里云百炼 / DashScope（Qwen3） | `{"enable_thinking": false}` |
| OpenRouter | `{"reasoning": {"enabled": false}}` |
| 部分中转站 / 网关 | `{"thinking": {"type": "disabled"}}` |
| OpenAI gpt-5 系列 | `{"reasoning_effort": "minimal"}` |
| Gemini 原生接口 | `{"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}` |

DeepSeek 的 `deepseek-reasoner` **无法关闭**思考，想要纯净输出请换 `deepseek-chat`。

设置页路径：模型配置 → 「思维链」下拉（`gui/widgets.py` 的 `THINKING_PRESETS`）
→ 选中即写入「额外参数」输入框，可再手动改。预设只是帮你写 JSON，不是隐式开关。

### 12.2 提示词硬约束（`config/config.py` 的 `PROMPT`）

把提示词写成"选项判定函数"：只输出一行 `答案：N`，明确禁止思考过程、解释、复述题目、
罗列选项（`1. 2. 3.`）以及"让我们一步步分析"这类开场。即使模型内部仍然推理，
也会被要求只把结论写进回复。这是**唯一对所有提供商都生效**的手段。

### 12.3 客户端解析与兜底（`tools/LLM/client.py`）

- `parse_choice()` 改为**标记优先 + 取最后一个**：先找 `答案 / 正确选项 / 应选 / 选 / answer`
  之后的数字，取最后一个落在选项范围内的；再退化到"文本里最后一个独立数字"。
  这条同时修掉了「思维链里出现 1. 2. 3. 被误读成选项 1」的根因。
- Gemini 的 `parts[].thought == true`（思考摘要）直接从正文里跳过。
- 正文为空但收到 `reasoning_content` / `reasoning` 时，用思考内容兜底解析
  （日志 `[LLM] reasoning-only#…`），避免整题白问。

对应测试：`test_llm_client.py::ReasoningContentTests`（思考链不混入正文、
正文为空时兜底、Gemini thought 分段被跳过）、`ParseChoiceTests::test_reasoning_style_outputs_pick_the_conclusion`、
`test_gui_presets.py`（预设合并、坏 JSON 被重建、首项不覆盖手写参数）。


