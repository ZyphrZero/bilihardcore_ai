#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页“思维链”预设的下拉行为测试（离屏渲染，不弹窗、不联网）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from PySide6.QtWidgets import QApplication
    import gui.widgets as widgets_module
    QT_AVAILABLE = True
except Exception:  # pragma: no cover - 没装 PySide6 时跳过
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, '未安装 PySide6，跳过 GUI 预设测试')
class ThinkingPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_widget(self):
        widget = widgets_module.ModelConfigWidget('custom')
        self.addCleanup(widget.deleteLater)
        return widget

    def test_preset_is_merged_into_extra_body(self):
        widget = self.make_widget()
        widget.extra_body_input.setText('{"temperature": 0}')
        # 找到“通用中转站”这一项，避免写死下标
        index = next(i for i, item in enumerate(widgets_module.THINKING_PRESETS)
                     if item[1] and 'thinking' in item[1])
        widget.thinking_combo.setCurrentIndex(index)

        options, error = widget._collect_options()
        self.assertIsNone(error)
        self.assertEqual(options['extra_body'],
                         {'temperature': 0, 'thinking': {'type': 'disabled'}})

    def test_broken_json_is_replaced_by_preset(self):
        widget = self.make_widget()
        widget.extra_body_input.setText('{不是 JSON')
        widget.thinking_combo.setCurrentIndex(1)

        options, error = widget._collect_options()
        self.assertIsNone(error)
        self.assertTrue(options['extra_body'])

    def test_first_option_keeps_handwritten_params(self):
        widget = self.make_widget()
        widget.extra_body_input.setText('{"top_p": 0.5}')
        widget.thinking_combo.setCurrentIndex(0)

        options, error = widget._collect_options()
        self.assertIsNone(error)
        self.assertEqual(options['extra_body'], {'top_p': 0.5})

    def test_presets_are_json_serializable_dicts(self):
        for label, payload, note in widgets_module.THINKING_PRESETS:
            with self.subTest(label=label):
                self.assertTrue(isinstance(payload, dict) or payload is None)
                self.assertTrue(note)
                if payload:
                    self.assertIsInstance(payload, dict)


if __name__ == '__main__':
    unittest.main()
