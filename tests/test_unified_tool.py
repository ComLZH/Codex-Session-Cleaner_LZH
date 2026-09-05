"""tests/test_unified_tool.py - 单元测试与真实环境只读扫描验证脚本"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from codex_engine import (
    SessionScanner,
    SessionState,
    StorageInventory,
    discover_storage_inventory,
    get_actual_volume,
    inspect_database_fingerprint,
    normalize_path,
    running_codex_processes,
)
from codex_console import parse_selection_indices


class TestUnifiedToolBasics(unittest.TestCase):
    def test_normalize_path(self) -> None:
        self.assertEqual(normalize_path(r"\\?\C:\Users\ComLZH\.codex"), "c:/users/comlzh/.codex")
        self.assertEqual(normalize_path("D:\\CrossGFW\\节点规则拼装系统\\"), "d:/crossgfw/节点规则拼装系统")
        self.assertEqual(normalize_path("c:/test/abc/"), "c:/test/abc")

    def test_parse_selection_indices_ascii(self) -> None:
        self.assertEqual(parse_selection_indices("1", 10), [1])
        self.assertEqual(parse_selection_indices("1,3,5", 10), [1, 3, 5])
        self.assertEqual(parse_selection_indices("2-4", 10), [2, 3, 4])
        self.assertEqual(parse_selection_indices("1, 3-5, 2", 10), [1, 3, 4, 5, 2])

    def test_parse_selection_indices_fullwidth_and_chinese(self) -> None:
        self.assertEqual(parse_selection_indices("１，３，５", 10), [1, 3, 5])
        self.assertEqual(parse_selection_indices("２～４", 10), [2, 3, 4])
        self.assertEqual(parse_selection_indices("1、2、3", 10), [1, 2, 3])
        self.assertEqual(parse_selection_indices("1——3", 10), [1, 2, 3])
        self.assertEqual(parse_selection_indices(" 1 ， 3 - 4 ", 10), [1, 3, 4])

    def test_parse_selection_indices_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_selection_indices("5-2", 10)
        with self.assertRaises(ValueError):
            parse_selection_indices("11", 10)
        with self.assertRaises(ValueError):
            parse_selection_indices("0", 10)
        with self.assertRaises(ValueError):
            parse_selection_indices("abc", 10)

    def test_volume_resolution(self) -> None:
        vol_c = get_actual_volume(Path("C:\\Users"))
        self.assertEqual(vol_c, "C:")


class TestRealEnvironmentReadOnlyScan(unittest.TestCase):
    def setUp(self) -> None:
        self.codex_root = Path.home() / ".codex"
        self.inventory = discover_storage_inventory(self.codex_root)

    def test_storage_inventory_discovery(self) -> None:
        self.assertTrue(self.inventory.resolved_root.exists())
        self.assertIsNotNone(self.inventory.codex_dev)
        if self.inventory.codex_dev:
            self.assertTrue(self.inventory.codex_dev.exists())

    def test_database_fingerprints(self) -> None:
        if self.inventory.codex_dev and self.inventory.codex_dev.exists():
            fp = inspect_database_fingerprint(self.inventory.codex_dev)
            self.assertTrue(fp.supported, f"codex-dev.db 指纹不兼容: {fp.status_detail}")

        if self.inventory.state_5_primary and self.inventory.state_5_primary.exists():
            fp = inspect_database_fingerprint(self.inventory.state_5_primary)
            self.assertTrue(fp.supported, f"state_5.sqlite 指纹不兼容: {fp.status_detail}")

    def test_scanner_real_run(self) -> None:
        scanner = SessionScanner(self.inventory)
        scan_id, items, warnings = scanner.scan_all()
        self.assertTrue(scan_id.startswith("202"))
        self.assertIsInstance(items, list)
        print(f"\n[测试信息] 扫描到真实会话数量: {len(items)}")
        for idx, item in enumerate(items[:5], 1):
            print(f"  样本 [{idx}] [{item.state.value}] {item.display_title[:20]} (ID: {item.thread_id[:8]})")


if __name__ == "__main__":
    unittest.main()
