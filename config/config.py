#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import tempfile
from loguru import logger

# 模型配置 - 基础URL和默认模型
MODEL_CONFIGS = {
    'deepseek': {
        'base_url': 'https://api.deepseek.com/v1',
        'model': 'deepseek-chat',
        'api_key': ''
    },
    'gemini': {
        'base_url': 'https://generativelanguage.googleapis.com/v1beta',
        'model': 'gemini-2.0-flash',
        'api_key': ''
    },
    'custom': {
        'base_url': '',
        'model': '',
        'api_key': ''
    }
}

# 模型显示信息
MODEL_DISPLAY_INFO = {
    'deepseek': {
        'name': 'DeepSeek',
        'icon': '🧠',
        'choice_value': '1'
    },
    'gemini': {
        'name': 'Gemini',
        'icon': '✨',
        'choice_value': '2'
    },
    'custom': {
        'name': '自定义模型',
        'icon': '⚙️',
        'choice_value': '3'
    }
}

# 配置目录
CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.bili-hardcore')


class ConfigWriteError(RuntimeError):
    """配置写盘失败（权限、占用、只读属性等），消息已是可读中文。"""


#: 程序所在目录
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 用户目录不可写时（受限进程 / 沙箱 / 只读安装目录）使用的回退配置目录。
#: 放在程序目录旁，保证"至少能存下配置"，日志里会明确说明改存到了哪里。
FALLBACK_CONFIG_DIR = os.path.join(_PROJECT_DIR, '.bili-hardcore')

# LLM 调用相关的默认参数（用户可在设置页覆盖）
DEFAULT_LLM_OPTIONS = {
    'connect_timeout': 10.0,   # 建连超时（秒）
    'read_timeout': 180.0,     # 读取超时：相邻两次收到数据的最大间隔（秒）
    'max_retries': 2,          # 超时/限流/5xx 的重试次数
    'stream': True,            # 是否启用流式输出（推荐开启）
    'extra_body': {},          # 额外请求参数，例如 {"thinking": {"type": "disabled"}}
}

def ensure_config_dir(directory=None):
    """确保配置目录存在"""
    os.makedirs(directory or CONFIG_DIR, exist_ok=True)

def _config_dirs():
    """候选配置目录：用户目录优先，其次程序目录旁的回退目录（去重）"""
    dirs = []
    for path in (CONFIG_DIR, FALLBACK_CONFIG_DIR):
        if path and path not in dirs:
            dirs.append(path)
    return dirs

def _config_file(model_type, directory=None):
    return os.path.join(directory or CONFIG_DIR, f'{model_type}_config.json')

def _existing_config_file(model_type):
    """已存在的配置文件路径（用户目录优先，其次回退目录）；都不存在时返回用户目录下的路径"""
    for directory in _config_dirs():
        candidate = _config_file(model_type, directory)
        if os.path.exists(candidate):
            return candidate
    return _config_file(model_type)

def _read_raw_config(model_type):
    """读取原始配置文件内容（保留未知字段，避免保存时丢数据）"""
    config_file = _existing_config_file(model_type)
    if not os.path.exists(config_file):
        return {}
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f'读取{model_type}配置失败: {e}')
        return {}

def _write_config_to(directory, model_type, data):
    """把配置原子写入指定目录"""
    ensure_config_dir(directory)
    config_file = _config_file(model_type, directory)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f'.{model_type}_config.', suffix='.tmp',
                                       dir=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_file)
        tmp_path = None
        if os.name == 'posix':
            # 配置里有 API Key，POSIX 上收紧到只有本人可读写。
            # Windows 上 chmod 只会改成"只读"属性，反而让后续删除/覆盖失败，所以跳过。
            try:
                os.chmod(config_file, 0o600)
            except OSError:  # pragma: no cover
                pass
        return config_file
    except PermissionError as e:
        raise ConfigWriteError(
            f'没有权限写入配置文件：{config_file}\n'
            f'系统返回：{e}\n\n'
            '常见原因：\n'
            '1) 该目录被杀毒软件的“受控文件夹访问/勒索软件防护”拦截；\n'
            '2) 程序运行在受限环境（沙箱/容器）里，只能写工作目录；\n'
            '3) 文件被设置为只读，或正被其他程序打开。\n'
            '原有配置没有被改动，不会损坏。'
        ) from e
    except OSError as e:
        raise ConfigWriteError(
            f'写入配置文件失败：{config_file}\n系统返回：{e}\n'
            '原有配置没有被改动，不会损坏。'
        ) from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:  # pragma: no cover
                pass

def load_model_config(model_type):
    """加载模型完整配置（包括API密钥）"""
    data = _read_raw_config(model_type)
    default_config = MODEL_CONFIGS.get(model_type, {})

    base_url = data.get('base_url')
    model_name = data.get('model')
    api_key = data.get('api_key')

    return {
        'base_url': default_config.get('base_url', '') if base_url is None else base_url,
        'model': default_config.get('model', '') if model_name is None else model_name,
        'api_key': default_config.get('api_key', '') if api_key is None else api_key,
    }

def load_llm_options(model_type):
    """加载 LLM 调用参数（超时/重试/流式/额外参数），缺失时用默认值"""
    data = _read_raw_config(model_type)
    stored = data.get('options')
    if not isinstance(stored, dict):
        stored = {}

    options = dict(DEFAULT_LLM_OPTIONS)

    def _as_float(key, minimum, maximum):
        value = stored.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        options[key] = min(max(value, minimum), maximum)

    def _as_int(key, minimum, maximum):
        value = stored.get(key)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        options[key] = min(max(value, minimum), maximum)

    _as_float('connect_timeout', 0.5, 120.0)
    _as_float('read_timeout', 1.0, 1800.0)
    _as_int('max_retries', 0, 10)
    if 'temperature' in stored:
        _as_float('temperature', 0.0, 2.0)
    if 'stream' in stored:
        options['stream'] = bool(stored.get('stream'))
    extra_body = stored.get('extra_body')
    if isinstance(extra_body, dict):
        options['extra_body'] = extra_body
    return options

def save_model_config(model_type, base_url=None, model_name=None, api_key='', options=None):
    """保存模型配置。

    只更新显式传入的字段，其余字段（包括用户手写的额外配置）原样保留；
    整个文件一次原子写入，不会出现"写了一半"的状态。
    """
    data = _read_raw_config(model_type)
    if base_url is not None:
        data['base_url'] = base_url
    if model_name is not None:
        data['model'] = model_name
    if api_key:
        data['api_key'] = api_key
    if options:
        merged = data.get('options')
        merged = merged if isinstance(merged, dict) else {}
        merged.update(options)
        data['options'] = merged

    errors = []
    saved_to = None
    for directory in _config_dirs():
        try:
            saved_to = _write_config_to(directory, model_type, data)
            break
        except ConfigWriteError as e:
            errors.append(str(e))

    if saved_to is None:
        logger.error(f'保存{model_type}配置失败: ' + '\n'.join(errors))
        raise ConfigWriteError(
            f'{model_type} 配置没能写入任何可用目录：\n\n' + '\n\n'.join(errors)
        )

    logger.info(f'{model_type}配置已保存: {saved_to}')
    if os.path.dirname(saved_to) != CONFIG_DIR:
        logger.warning(
            f'配置目录 {CONFIG_DIR} 不可写，配置已改存到 {saved_to}。'
            '程序读取时会优先用这个文件，所以设置依然生效。'
        )
    return saved_to

def load_api_key(key_type):
    """加载API密钥（从统一配置文件中）"""
    config = load_model_config(key_type)
    return config.get('api_key', '')

def save_api_key(key_type, api_key):
    """保存API密钥（到统一配置文件中）"""
    save_model_config(key_type, api_key=api_key)

# 项目配置
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# B站API配置
API_CONFIG = {
    'appkey': '783bbb7264451d82',
    'appsec': '2653583c8873dea268ab9386918b1d65',
    'user_agent': 'Mozilla/5.0 BiliDroid/1.12.0 (bbcallen@gmail.com)',
}

HEADERS = {
    'User-Agent': API_CONFIG['user_agent'],
    'Content-Type': 'application/x-www-form-urlencoded',
    'Accept': 'application/json',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'x-bili-metadata-legal-region': 'CN',
    'x-bili-aurora-eid': '',
    'x-bili-aurora-zone': '',
}

AUTH_FILE = os.path.join(CONFIG_DIR, 'auth.json')

# LLM提示词模板
PROMPT = '''
当前时间：{}
你是答题程序里的一个"选项判定"函数：输入一道单选题和 4 个选项，只输出最终选项的序号（1/2/3/4）。

输出规则（必须严格遵守）：
1. 只输出一行，格式固定为 `答案：N`，N 是 1 到 4 之间的一个数字。
2. 不要输出思考过程、推理步骤、解释、理由、复述题目或罗列选项。
3. 不要使用 1. 2. 3. 这样的列表格式，也不要输出多个序号。
4. 即使你内部会先推理，也只把最终结论写进回复里，推理内容不要出现在回复中。
5. 不确定时也必须给出一个序号（选最可能的那个），不要回答"无法确定"。

示例：
问题：大的反义词是什么？
选项：['长', '宽', '小', '热']
答案：3
---
请回答我的问题：{}
'''

# GUI模式全局变量
model_choice = '1'  # 默认DeepSeek

# 初始化配置
ensure_config_dir()