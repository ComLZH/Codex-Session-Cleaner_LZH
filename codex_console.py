"""codex_console.py - Codex 会话管理与清理工具交互控制台主程序

模块功能：
1. 控制台 UI 渲染与项目/状态分类树状展示；
2. NFKC 中文输入法全角字符容错解析器 (parse_selection_indices)；
3. Dry-run 计划生成、目标指纹核对与短操作码动态确认短语；
4. 删除级进程守门、排他锁校验与 WAL 状态确认；
5. 独立只读 quick_check 健康诊断与 VACUUM 空间维护。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from codex_engine import (
    AUTHOR_TAG,
    ENGINE_VERSION,
    BackupManager,
    ExecutionEngine,
    QuarantineManager,
    SessionItem,
    SessionScanner,
    SessionState,
    SingleInstanceMutex,
    StorageInventory,
    checkpoint_wal_database,
    check_exclusive_file_access,
    connect_readonly,
    discover_storage_inventory,
    inspect_database_fingerprint,
    running_codex_processes,
)


def configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"Codex-Session-Cleaner_{AUTHOR_TAG} v{ENGINE_VERSION}")
        except Exception:
            pass


def parse_selection_indices(raw_input: str, max_valid: int) -> List[int]:
    """解析用户输入的数字选择表达式 (支持单号 1、列表 1,3,4、区间 2-5)

    内置 Unicode NFKC 全角字符预清洗与常见中文输入法标点容错。
    """
    if not raw_input or not raw_input.strip():
        return []

    # 1. NFKC 规范化 (全角数字 -> 半角数字, 全角逗号 -> 半角逗号等)
    s = unicodedata.normalize("NFKC", raw_input)
    s = s.translate(str.maketrans({
        "、": ",",
        ";": ",",
        "；": ",",
        "~": "-",
        "～": "-",
        "—": "-",
        "–": "-",
        "－": "-",
    }))
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"-+", "-", s)

    # 2. 语法校验与拆解
    selected: List[int] = []
    seen: Set[int] = set()
    parts = s.split(",")

    for part in parts:
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise ValueError(f"无效的区间语法: '{part}'")
            try:
                start_i = int(bounds[0])
                end_i = int(bounds[1])
            except ValueError:
                raise ValueError(f"区间数字无效: '{part}'")
            if start_i > end_i:
                raise ValueError(f"倒序区间无效: '{part}'")
            for idx in range(start_i, end_i + 1):
                if idx < 1 or idx > max_valid:
                    raise ValueError(f"编号 {idx} 超出当前有效菜单范围 (1~{max_valid})")
                if idx not in seen:
                    selected.append(idx)
                    seen.add(idx)
        else:
            try:
                idx = int(part)
            except ValueError:
                raise ValueError(f"非数字输入: '{part}'")
            if idx < 1 or idx > max_valid:
                raise ValueError(f"编号 {idx} 超出当前有效菜单范围 (1~{max_valid})")
            if idx not in seen:
                selected.append(idx)
                seen.add(idx)

    return selected


class UnifiedConsoleApp:
    def __init__(self, codex_root: Optional[Path] = None) -> None:
        if codex_root is None:
            home = Path.home()
            codex_root = home / ".codex"
        self.codex_root = codex_root
        self.inventory: StorageInventory = discover_storage_inventory(self.codex_root)
        self.scanner = SessionScanner(self.inventory)
        self.engine = ExecutionEngine(self.inventory)
        self.mutex = SingleInstanceMutex()

        self.current_scan_id: str = ""
        self.current_items: List[SessionItem] = []
        self.scan_warnings: List[str] = []
        self.last_scan_time: Optional[dt.datetime] = None

    def refresh_scan(self) -> None:
        self.current_scan_id, self.current_items, self.scan_warnings = self.scanner.scan_all()
        self.last_scan_time = dt.datetime.now()

    def print_banner(self) -> None:
        print("=" * 80)
        print(f"       Codex 会话管理与清理工具 (Codex-Session-Cleaner_{AUTHOR_TAG} v{ENGINE_VERSION})")
        print("=" * 80)

        procs = running_codex_processes()
        proc_status = (
            f"正在运行 (检测到 {len(procs)} 个进程，当前仅允许只读扫描)"
            if procs
            else "已完全退出 (安全，具备删除权限)"
        )
        print(f"【作用范围】当前用户: {os.getlogin() if hasattr(os, 'getlogin') else '当前用户'} | 根路径: {self.inventory.resolved_root}")
        print(f"【进程状态】{proc_status}")

        fps = []
        for db in (self.inventory.codex_dev, self.inventory.state_5_primary):
            if db and db.exists():
                fp = inspect_database_fingerprint(db)
                fps.append(f"{db.name}: {'兼容' if fp.supported else '不兼容(只读)'}")
        print(f"【结构兼容】{' | '.join(fps) if fps else '未发现核心数据库'}")

        if self.last_scan_time:
            time_str = self.last_scan_time.strftime("%H:%M:%S")
            ghost_count = sum(1 for it in self.current_items if it.state == SessionState.VERIFIED_GHOST)
            healthy_count = sum(1 for it in self.current_items if it.state == SessionState.HEALTHY_LOCAL)
            cloud_count = sum(1 for it in self.current_items if it.state == SessionState.CLOUD_READONLY)
            print(f"【会话统计】总计 {len(self.current_items)} 条 | 正常: {healthy_count} | 幽灵残留: {ghost_count} | 云端: {cloud_count} | 快照: {self.current_scan_id[-8:]} ({time_str})")
        print("-" * 80)

    def display_session_list(self) -> None:
        if not self.current_items:
            print("当前未发现任何本地或云端会话记录。")
            return

        grouped: Dict[str, List[Tuple[int, SessionItem]]] = {}
        for idx, item in enumerate(self.current_items, start=1):
            key = item.project_display
            if key not in grouped:
                grouped[key] = []
            grouped[key].append((idx, item))

        for proj, items_in_proj in grouped.items():
            print(f"\n📁 项目: {proj} (共 {len(items_in_proj)} 条记录)")
            for idx, it in items_in_proj:
                state_tag = f"[{it.state.value}]"
                if it.state == SessionState.VERIFIED_GHOST:
                    state_tag = "【幽灵残留】"
                elif it.state == SessionState.HEALTHY_LOCAL:
                    state_tag = "[正常完整]"
                elif it.state == SessionState.CLOUD_READONLY:
                    state_tag = "[云端同步]"
                elif it.state == SessionState.BROKEN_BODY:
                    state_tag = "【正文缺失】"

                title_disp = (it.display_title[:28] + "...") if len(it.display_title) > 30 else it.display_title
                print(f"  [{idx:2d}] {state_tag:<8} {title_disp:<32} (ID: {it.thread_id[:8]}...)")

    def show_item_evidence(self, index_or_tid: str) -> None:
        target: Optional[SessionItem] = None
        if index_or_tid.isdigit():
            idx = int(index_or_tid)
            if 1 <= idx <= len(self.current_items):
                target = self.current_items[idx - 1]
        if target is None:
            for it in self.current_items:
                if it.thread_id.lower().startswith(index_or_tid.lower()):
                    target = it
                    break

        if not target:
            print(f"❌ 未找到对应的会话记录: '{index_or_tid}'")
            return

        print("\n" + "=" * 60)
        print(f"会话证据明细: {target.display_title}")
        print("=" * 60)
        print(f"  Thread ID      : {target.thread_id}")
        print(f"  Host ID        : {target.host_id}")
        print(f"  工作目录 (cwd) : {target.cwd or '无'}")
        print(f"  状态分类       : {target.state.value} ({target.state_reason})")
        print(f"  catalog 目录行 : {'存在' if target.has_catalog_entry else '不存在'} (missing_candidate={target.missing_candidate})")
        print(f"  state_5 任务行 : {'存在' if target.has_state_entry else '不存在'}")
        print(f"  历史记录条目   : {'存在' if target.has_history_entry else '不存在'}")
        print(f"  会话索引行     : {'存在' if target.has_session_index_entry else '不存在'}")
        print(f"  物理 rollout   : {'存在' if target.rollout_exists else '缺失'} ({target.rollout_path or '无路径'})")
        print(f"  写入锁文件     : {'存在锁定' if target.has_lock_file else '无锁'}")
        print(f"  目标校验指纹   : {target.target_fingerprint}")
        print("=" * 60)

    def dry_run_and_confirm_cleanup(self, targets: List[SessionItem]) -> None:
        if not targets:
            print("没有可执行的清理目标。")
            return

        cloud_targets = [t for t in targets if t.state == SessionState.CLOUD_READONLY]
        if cloud_targets:
            print("❌ 阻断：所选列表中包含云端同步会话，本工具严格禁止删除云端会话！")
            return

        procs = running_codex_processes()
        if procs:
            print("\n" + "!" * 70)
            print("【删除操作被阻止】检测到 Codex 正在运行！")
            print("必须先完全退出 Codex 桌面客户端（包括系统托盘）后，才能执行永久删除。")
            for p in procs:
                print(f"  - 进程: {p['image_name']} (PID: {p['pid']})")
            print("!" * 70)
            return

        if not self.mutex.acquire():
            print("❌ 阻断：检测到已有另一个清理工具实例在运行中，禁止并发写入！")
            return

        try:
            for db in (self.inventory.codex_dev, self.inventory.state_5_primary):
                if db and db.exists():
                    ok, busy, log_f, ckpt_f = checkpoint_wal_database(db)
                    if not ok or busy > 0:
                        print(f"❌ 阻断：数据库 {db.name} WAL 检查点未完成 (busy={busy})，可能有其他应用占用！")
                        return

            for db in (self.inventory.codex_dev, self.inventory.state_5_primary, self.inventory.thread_history_1):
                if db and db.exists():
                    if not check_exclusive_file_access(db):
                        print(f"❌ 阻断：数据库 {db.name} 被操作系统排他性锁定，无法安全访问！")
                        return

            op_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S_") + hashlib.md5(os.urandom(8)).hexdigest()[:6].upper()
            short_code = hashlib.md5(f"{op_id}_{len(targets)}".encode("utf-8")).hexdigest()[:8].upper()

            print("\n" + "=" * 70)
            print(f"           Dry-run 执行计划预审 (操作码: {short_code})")
            print("=" * 70)
            print(f"操作 ID      : {op_id}")
            print(f"目标会话数  : {len(targets)} 条")
            print(f"目标会话清单:")
            for t in targets:
                print(f"  - [{t.state.value}] {t.display_title} (ID: {t.thread_id})")

            print("\n预计影响范围:")
            print(f"  1. 数据库写入 : codex-dev.db, state_5.sqlite, thread_history_1.sqlite 等关联行删除")
            print(f"  2. 文本索引   : 从 session_index.jsonl 移除对应行，修剪 .codex-global-state.json")
            print(f"  3. 物理隔离   : 将存在的 rollout 物理文件移入同物理卷专用隔离区 (不直接物理抹除)")
            print(f"  4. 冷备份位置 : %LOCALAPPDATA%\\CodexCleanup\\backups\\{op_id}\\ (Connection.backup 快照)")
            print("=" * 70)

            expected_phrase = f"确认删除{len(targets)}个本机会话 {short_code}"
            print(f"⚠️  高安全级别防误触提示：")
            print(f"如果确认执行上述操作，请精确输入以下确认短语（含空格和操作码）：")
            print(f"  👉 {expected_phrase}")
            print("输入其他任何内容、按 Ctrl+C 或直接按回车均会安全取消本次操作。")

            user_input = input("\n请输入确认短语: ").strip()
            if user_input != expected_phrase:
                print("❌ 确认短语不匹配，本次删除操作已安全取消。")
                return

            print("\n[1/4] 正在建立 SQLite 一致性快照冷备份与 SHA-256 清单...")
            backup_mgr = BackupManager(op_id)
            quarantine_mgr = QuarantineManager(op_id)

            print("[2/4] 正在分库执行拓扑事务删除并校验外键一致性...")
            report = self.engine.execute_plan(targets, backup_mgr, quarantine_mgr)

            print("[3/4] 正在更新文本状态索引并移动物理文件至隔离区...")
            reports_dir = Path(__file__).resolve().parent / "reports"
            reports_dir.mkdir(exist_ok=True)
            report_file = reports_dir / f"cleanup_{op_id}.json"
            report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

            print("[4/4] 执行完毕！正在刷新会话快照...")
            self.refresh_scan()

            print("\n" + "✨" * 35)
            print("🎉 清理操作成功完成！")
            print(f"  - 备份目录: {backup_mgr.base_dir}")
            print(f"  - 审计报告: {report_file}")
            print("  - 提示: 物理文件已安全隔离。你可以随时重新启动 Codex 桌面端核验侧边栏效果。")
            print("✨" * 35)

        finally:
            self.mutex.release()

    def run_vacuum_maintenance(self) -> None:
        procs = running_codex_processes()
        if procs:
            print("❌ 执行空间维护前必须先完全关闭 Codex！")
            return
        print("\n正在对关键 SQLite 数据库执行 VACUUM 碎片回收，请稍候...")
        for db in (self.inventory.codex_dev, self.inventory.state_5_primary, self.inventory.thread_history_1):
            if db and db.exists():
                try:
                    conn = sqlite3.connect(str(db.resolve()), timeout=60)
                    conn.execute("VACUUM")
                    conn.close()
                    print(f"  ✔️ {db.name} 空间压缩完成")
                except Exception as e:
                    print(f"  ❌ {db.name} 压缩失败: {e}")
        print("空间整理完成。\n")

    def run_health_check(self) -> None:
        print("\n正在对所有数据库执行只读 quick_check 检查...")
        for db in (
            self.inventory.codex_dev,
            self.inventory.state_5_primary,
            self.inventory.state_5_compat,
            self.inventory.thread_history_1,
            self.inventory.logs_2_primary,
        ):
            if db and db.exists():
                try:
                    conn = connect_readonly(db, timeout=5.0)
                    row = conn.execute("PRAGMA quick_check").fetchone()
                    conn.close()
                    res = row[0] if row else "unknown"
                    print(f"  {'✔️' if res == 'ok' else '❌'} {db.name:<25}: {res}")
                except Exception as e:
                    print(f"  ❌ {db.name:<25}: 检查失败 ({e})")
        print("检查完成。\n")

    def run(self) -> None:
        configure_console_output()
        self.refresh_scan()

        while True:
            self.print_banner()
            print("【可用操作菜单】")
            print("  [1] 重新扫描当前数据根")
            print("  [2] 查看当前所有项目与会话清单")
            print("  [3] 查看指定会话的完整底层证据 (输入编号或 Thread ID)")
            print("  [4] 一键清理所有【侧边栏幽灵残留】(推荐，快速安全)")
            print("  [5] 交互勾选会话删除 (支持输入编号: 1,3 或区间 2-5)")
            print("  [6] 输入特定 Thread ID 定向清除")
            print("  [7] SQLite 与全局状态只读健康检查 (quick_check)")
            print("  [8] 可选数据库空间维护 (VACUUM 碎片回收)")
            print("  [0] 退出工具")
            print("=" * 80)

            try:
                raw_c = input("请输入操作指令编号: ").strip()
                choice = re.sub(r"[^\w\-]", "", raw_c)
            except (EOFError, KeyboardInterrupt):
                print("\n检测到退出信号，已退出。再见！")
                break
            if choice == "0":
                print("\n已退出 Codex 会话管理工具。再见！")
                break
            elif choice == "1":
                self.refresh_scan()
                print("\n✔️ 扫描已刷新！\n")
            elif choice == "2":
                self.display_session_list()
                input("\n按回车键返回主菜单...")
            elif choice == "3":
                inp = input("请输入会话编号或 Thread ID: ").strip()
                self.show_item_evidence(inp)
                input("\n按回车键返回主菜单...")
            elif choice == "4":
                ghosts = [it for it in self.current_items if it.state == SessionState.VERIFIED_GHOST]
                if not ghosts:
                    print("\n🎉 当前未发现任何【侧边栏幽灵残留】记录！无需清理。\n")
                else:
                    self.dry_run_and_confirm_cleanup(ghosts)
                input("\n按回车键返回主菜单...")
            elif choice == "5":
                self.display_session_list()
                print("\n请输入要删除的会话编号 (支持半角/全角逗号分隔多选，如: 1,3 或区间 2-4):")
                sel_str = input("选择编号: ").strip()
                try:
                    indices = parse_selection_indices(sel_str, len(self.current_items))
                    if not indices:
                        print("未选择任何有效编号。")
                    else:
                        selected_items = [self.current_items[i - 1] for i in indices]
                        self.dry_run_and_confirm_cleanup(selected_items)
                except ValueError as e:
                    print(f"❌ 输入格式错误: {e}")
                input("\n按回车键返回主菜单...")
            elif choice == "6":
                tid = input("请输入完整的 36 位 Thread ID: ").strip()
                matched = [it for it in self.current_items if it.thread_id.lower() == tid.lower()]
                if not matched:
                    print(f"❌ 未在当前记录中检索到 Thread ID: {tid}")
                else:
                    self.dry_run_and_confirm_cleanup(matched)
                input("\n按回车键返回主菜单...")
            elif choice == "7":
                self.run_health_check()
                input("\n按回车键返回主菜单...")
            elif choice == "8":
                self.run_vacuum_maintenance()
                input("\n按回车键返回主菜单...")
            else:
                print(f"⚠️  未知选项: '{choice}'，请重新输入。")


if __name__ == "__main__":
    app = UnifiedConsoleApp()
    app.run()
