#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""配置读写（原子写 / 字段保留 / 参数夹取）的单元测试。

所有读写都发生在临时目录里，不会碰到真实的 ~/.bili-hardcore 配置。
"""

import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.config as app_config  # noqa: E402
from config.config import (DEFAULT_LLM_OPTIONS,  # noqa: E402
                          load_llm_options, load_model_config,
                          save_model_config)

#: 临时配置目录固定在仓库内（系统临时目录 / 运行时新建的子目录在部分沙箱下不可写）
TMP_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_config')


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_BASE, exist_ok=True)
        # 每个用例开始前清空，保证用例之间互不影响
        for name in os.listdir(TMP_BASE):
            target = os.path.join(TMP_BASE, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.remove(target)
                except OSError:
                    pass
        patcher = mock.patch.object(app_config, 'CONFIG_DIR', TMP_BASE)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 回退目录也要指到临时目录，否则用例会把配置写进仓库根目录
        fallback_dir = os.path.join(TMP_BASE, 'fallback')
        fallback_patcher = mock.patch.object(app_config, 'FALLBACK_CONFIG_DIR', fallback_dir)
        fallback_patcher.start()
        self.addCleanup(fallback_patcher.stop)

    # ---------- 工具 ----------

    def _config_path(self, model_type='custom'):
        return os.path.join(TMP_BASE, f'{model_type}_config.json')

    def _read_file(self, model_type='custom'):
        with open(self._config_path(model_type), 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_file(self, data, model_type='custom'):
        with open(self._config_path(model_type), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    # ---------- 用例 ----------

    def test_roundtrip_all_fields(self):
        save_model_config('custom', base_url='https://api.example.com/v1',
                          model_name='my-model', api_key='sk-123')
        loaded = load_model_config('custom')
        self.assertEqual(loaded['base_url'], 'https://api.example.com/v1')
        self.assertEqual(loaded['model'], 'my-model')
        self.assertEqual(loaded['api_key'], 'sk-123')

    def test_saving_api_key_keeps_other_fields(self):
        """回归测试：旧实现分两次写，且地址/模型为空时静默跳过，
        导致"只改 API Key"会把地址和模型丢掉（或根本没保存）。"""
        save_model_config('custom', base_url='https://api.example.com/v1',
                          model_name='my-model', api_key='sk-old',
                          options={'read_timeout': 90.0})
        save_model_config('custom', api_key='sk-new')

        loaded = load_model_config('custom')
        self.assertEqual(loaded['api_key'], 'sk-new')
        self.assertEqual(loaded['base_url'], 'https://api.example.com/v1')
        self.assertEqual(loaded['model'], 'my-model')
        self.assertEqual(load_llm_options('custom')['read_timeout'], 90.0)

    def test_unknown_fields_are_preserved(self):
        self._write_file({'api_key': 'sk-1', 'user_note': '手写的备注', 'options': {'custom_flag': True}})
        save_model_config('custom', model_name='m2')

        raw = self._read_file()
        self.assertEqual(raw['user_note'], '手写的备注')
        self.assertEqual(raw['options']['custom_flag'], True)
        self.assertEqual(raw['model'], 'm2')
        self.assertEqual(raw['api_key'], 'sk-1')

    def test_options_are_merged_not_replaced(self):
        save_model_config('custom', options={'read_timeout': 120.0})
        save_model_config('custom', options={'max_retries': 5})

        options = load_llm_options('custom')
        self.assertEqual(options['read_timeout'], 120.0)
        self.assertEqual(options['max_retries'], 5)

    def test_no_temp_files_left_behind(self):
        save_model_config('custom', base_url='https://api.example.com/v1',
                          model_name='m', api_key='sk-1')
        leftovers = [name for name in os.listdir(TMP_BASE) if name.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_defaults_when_file_missing(self):
        loaded = load_model_config('custom')
        default_config = app_config.MODEL_CONFIGS.get('custom', {})
        self.assertEqual(loaded['base_url'], default_config.get('base_url', ''))
        self.assertEqual(loaded['model'], default_config.get('model', ''))
        self.assertEqual(loaded['api_key'], default_config.get('api_key', ''))

        options = load_llm_options('custom')
        for key, value in DEFAULT_LLM_OPTIONS.items():
            self.assertEqual(options[key], value)

    def test_options_are_clamped_on_load(self):
        self._write_file({'options': {
            'read_timeout': 99999,     # 上限 1800
            'connect_timeout': 0.01,   # 下限 0.5
            'max_retries': -3,         # 下限 0
            'temperature': 9,          # 上限 2
            'stream': 0,
            'extra_body': {'thinking': {'type': 'disabled'}},
        }})
        options = load_llm_options('custom')
        self.assertEqual(options['read_timeout'], 1800.0)
        self.assertEqual(options['connect_timeout'], 0.5)
        self.assertEqual(options['max_retries'], 0)
        self.assertEqual(options['temperature'], 2.0)
        self.assertFalse(options['stream'])
        self.assertEqual(options['extra_body'], {'thinking': {'type': 'disabled'}})

    def test_invalid_option_values_fall_back_to_default(self):
        self._write_file({'options': {'read_timeout': '不是数字', 'max_retries': None}})
        options = load_llm_options('custom')
        self.assertEqual(options['read_timeout'], DEFAULT_LLM_OPTIONS['read_timeout'])
        self.assertEqual(options['max_retries'], DEFAULT_LLM_OPTIONS['max_retries'])

    def test_broken_json_file_does_not_crash(self):
        with open(self._config_path('custom'), 'w', encoding='utf-8') as f:
            f.write('{ this is not json')
        self.assertEqual(load_model_config('custom')['api_key'],
                         app_config.MODEL_CONFIGS.get('custom', {}).get('api_key', ''))
        save_model_config('custom', api_key='sk-after-broken')
        self.assertEqual(load_model_config('custom')['api_key'], 'sk-after-broken')

    def test_save_api_key_only(self):
        app_config.save_api_key('gemini', 'sk-gemini')
        self.assertEqual(app_config.load_api_key('gemini'), 'sk-gemini')

    def test_permission_error_becomes_readable_config_write_error(self):
        """两个目录都写不进去时，抛可读中文的 ConfigWriteError，且旧配置保持不变。"""
        save_model_config('custom', base_url='https://old.example.com/v1',
                          model_name='old-model', api_key='sk-old')

        with mock.patch.object(app_config.tempfile, 'mkstemp',
                               side_effect=PermissionError(13, 'Permission denied', 'x.tmp')):
            with self.assertRaises(app_config.ConfigWriteError) as ctx:
                save_model_config('custom', api_key='sk-new')

        message = str(ctx.exception)
        self.assertIn('没有权限写入配置文件', message)
        self.assertIn('原有配置没有被改动', message)
        # 旧配置没有被破坏
        stored = load_model_config('custom')
        self.assertEqual(stored['base_url'], 'https://old.example.com/v1')
        self.assertEqual(stored['api_key'], 'sk-old')

    def test_other_oserror_is_wrapped_too(self):
        with mock.patch.object(app_config.tempfile, 'mkstemp',
                               side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(app_config.ConfigWriteError) as ctx:
                save_model_config('custom', api_key='sk-x')
        self.assertIn('写入配置文件失败', str(ctx.exception))

    def test_save_falls_back_when_user_dir_is_not_writable(self):
        """用户目录不可写时自动改存到回退目录，并且还能读回来。"""
        real_write = app_config._write_config_to

        def fake_write(directory, model_type, data):
            if directory == app_config.CONFIG_DIR:
                raise app_config.ConfigWriteError(
                    f'没有权限写入配置文件：{app_config.CONFIG_DIR}\n原有配置没有被改动，不会损坏。')
            return real_write(directory, model_type, data)

        with mock.patch.object(app_config, '_write_config_to', side_effect=fake_write):
            save_model_config('custom', base_url='https://fb.example.com/v1',
                              model_name='fb-model', api_key='sk-fallback',
                              options={'read_timeout': 90})

        # 回退目录里确实有文件
        fallback_file = os.path.join(app_config.FALLBACK_CONFIG_DIR, 'custom_config.json')
        self.assertTrue(os.path.exists(fallback_file), f'回退文件不存在: {fallback_file}')

        # 读取（仍在回退目录）能拿到刚存的值
        stored = load_model_config('custom')
        self.assertEqual(stored['base_url'], 'https://fb.example.com/v1')
        self.assertEqual(stored['api_key'], 'sk-fallback')
        self.assertEqual(load_llm_options('custom')['read_timeout'], 90.0)


if __name__ == '__main__':
    unittest.main()
