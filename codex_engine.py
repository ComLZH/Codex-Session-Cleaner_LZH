"""codex_engine.py - Codex 会话管理与清理工具核心底层引擎

模块功能：
1. 存储发现、数据库指纹提取与结构版本兼容性门禁 (Fail-Closed)；
2. Windows API 进程树枚举、独占句柄锁检测、单实例互斥锁与 WAL 受检 checkpoint；
3. 多源证据收集与互斥会话健康度分类器 (SessionScanner)；
4. 基于 sqlite3.Connection.backup() 的一致性数据库快照冷备份与 SHA-256 清单管理 (BackupManager)；
5. 基于物理卷自适应的同卷原子隔离区管理器 (QuarantineManager)；
6. 分库拓扑事务级联清理与后置 Integrity 校验执行器 (ExecutionEngine)。
"""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ENGINE_VERSION = "2.1.1"
AUTHOR_TAG = "LZH"


# ===========================================================================
# 1. 存储清单与数据库结构指纹提取
# ===========================================================================

@dataclass(frozen=True)
class StorageInventory:
    codex_root: Path
    resolved_root: Path
    root_volume: str
    state_5_primary: Optional[Path]
    state_5_compat: Optional[Path]
    logs_2_primary: Optional[Path]
    logs_2_compat: Optional[Path]
    codex_dev: Optional[Path]
    thread_history_1: Optional[Path]
    auxiliary_databases: Tuple[Path, ...]
    sidecars: Tuple[Path, ...]
    global_state_json: Optional[Path]
    global_state_bak: Optional[Path]
    session_index_jsonl: Optional[Path]
    sessions_dir: Optional[Path]
    writer_locks_dir: Optional[Path]
    visualizations_dir: Optional[Path]
    config_toml: Optional[Path]


@dataclass(frozen=True)
class DatabaseFingerprint:
    path: Path
    exists: bool
    journal_mode: str
    migration_version: Optional[str]
    tables: Dict[str, Tuple[str, ...]]
    unknown_tables_with_thread_ref: Tuple[str, ...]
    supported: bool
    status_detail: str


# 已知且受支持的官方基线表结构定义
KNOWN_BASELINE_SCHEMAS: Dict[str, Dict[str, Set[str]]] = {
    "codex-dev.db": {
        "local_thread_catalog": {"host_id", "thread_id", "display_title", "cwd", "missing_candidate"},
        "thread_timeline_ledger": {"thread_id"},
        "inbox_items": {"thread_id"},
        "automation_runs": {"thread_id"},
        "automations": {"target_thread_id"},
        "local_thread_catalog_scan_entries": {"thread_id"},
        "local_thread_catalog_metadata": set(),
        "local_thread_catalog_hosts": set(),
        "local_thread_catalog_sync_state": set(),
        "local_thread_catalog_scan_checkpoints": set(),
        "local_app_server_feature_enablement": set(),
        "codex_schema_migrations": set(),
    },
    "state_5.sqlite": {
        "threads": {"id", "rollout_path", "cwd", "title"},
        "projects": {"id", "name"},
        "project_roots": {"project_id", "path"},
        "thread_spawn_edges": set(),
        "thread_artifacts": set(),
        "thread_dynamic_tools": set(),
    },
    "thread_history_1.sqlite": {
        "thread_turns": {"thread_id", "turn_id"},
        "thread_items": {"thread_id", "turn_id", "item_id"},
        "thread_realtime_items": {"thread_id"},
        "thread_history_projection_state": {"thread_id"},
        "_sqlx_migrations": set(),
    },
    "logs_2.sqlite": {
        "logs": {"thread_id"},
    },
}

THREAD_REF_COL_NAMES = {"thread_id", "assigned_thread_id", "target_thread_id"}


def connect_readonly(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    """以只读模式连接 SQLite 数据库，并设置有界等待与只读安全标记"""
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def discover_storage_inventory(codex_root: Path) -> StorageInventory:
    """枚举并清点当前 .codex 目录下的所有关键存储位置及 sidecar 文件"""
    resolved_root = codex_root.resolve(strict=False)
    root_volume = os.path.splitdrive(str(resolved_root))[0].upper()

    def opt_path(rel: str) -> Optional[Path]:
        p = codex_root / rel
        return p if p.exists() else None

    state_5_p = opt_path("state_5.sqlite")
    state_5_c = opt_path("sqlite/state_5.sqlite")
    logs_2_p = opt_path("logs_2.sqlite")
    logs_2_c = opt_path("sqlite/logs_2.sqlite")
    codex_dev = opt_path("sqlite/codex-dev.db")
    thread_hist = opt_path("thread_history_1.sqlite")

    aux_candidates = [
        codex_root / "goals_1.sqlite",
        codex_root / "memories_1.sqlite",
        codex_root / "queue_1.sqlite",
        codex_root / "sqlite" / "goals_1.sqlite",
        codex_root / "sqlite" / "memories_1.sqlite",
    ]
    aux_dbs = tuple(p for p in aux_candidates if p.exists())

    all_dbs = [state_5_p, state_5_c, logs_2_p, logs_2_c, codex_dev, thread_hist, *aux_dbs]
    sidecars: List[Path] = []
    for db in all_dbs:
        if db is not None:
            wal = Path(str(db) + "-wal")
            shm = Path(str(db) + "-shm")
            if wal.exists():
                sidecars.append(wal)
            if shm.exists():
                sidecars.append(shm)

    return StorageInventory(
        codex_root=codex_root,
        resolved_root=resolved_root,
        root_volume=root_volume,
        state_5_primary=state_5_p,
        state_5_compat=state_5_c,
        logs_2_primary=logs_2_p,
        logs_2_compat=logs_2_c,
        codex_dev=codex_dev,
        thread_history_1=thread_hist,
        auxiliary_databases=aux_dbs,
        sidecars=tuple(sidecars),
        global_state_json=opt_path(".codex-global-state.json"),
        global_state_bak=opt_path(".codex-global-state.json.bak"),
        session_index_jsonl=opt_path("session_index.jsonl"),
        sessions_dir=opt_path("sessions"),
        writer_locks_dir=opt_path("thread-writer-locks"),
        visualizations_dir=opt_path("visualizations"),
        config_toml=opt_path("config.toml"),
    )


def inspect_database_fingerprint(db_path: Path) -> DatabaseFingerprint:
    """提取数据库结构指纹并判断是否受支持（Fail-Closed 门禁机制）"""
    if not db_path.exists():
        return DatabaseFingerprint(
            path=db_path,
            exists=False,
            journal_mode="",
            migration_version=None,
            tables={},
            unknown_tables_with_thread_ref=(),
            supported=True,
            status_detail="文件不存在（安全跳过）",
        )

    try:
        conn = connect_readonly(db_path, timeout=5.0)
    except sqlite3.OperationalError as e:
        return DatabaseFingerprint(
            path=db_path,
            exists=True,
            journal_mode="unknown",
            migration_version=None,
            tables={},
            unknown_tables_with_thread_ref=(),
            supported=False,
            status_detail=f"数据库忙或无法读取 (TEMPORARILY_BUSY): {e}",
        )

    try:
        jm_row = conn.execute("PRAGMA journal_mode").fetchone()
        journal_mode = str(jm_row[0]).lower() if jm_row else "unknown"

        migration_ver: Optional[str] = None
        for mig_table in ("_sqlx_migrations", "codex_schema_migrations"):
            has_mig = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (mig_table,)
            ).fetchone()
            if has_mig:
                ver_col = "version" if mig_table == "_sqlx_migrations" else "migration_number"
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{mig_table}')").fetchall()]
                if ver_col in cols:
                    row = conn.execute(f"SELECT max({ver_col}) FROM '{mig_table}'").fetchone()
                    if row and row[0] is not None:
                        migration_ver = f"{mig_table}:{row[0]}"
                        break

        tables_dict: Dict[str, Tuple[str, ...]] = {}
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for r in rows:
            tname = r[0]
            col_rows = conn.execute(f'PRAGMA table_info("{tname}")').fetchall()
            tables_dict[tname] = tuple(c[1] for c in col_rows)

        db_key = db_path.name.lower()
        baseline = KNOWN_BASELINE_SCHEMAS.get(db_key, {})
        unknown_with_refs: List[str] = []

        for tname, cols in tables_dict.items():
            if tname not in baseline:
                cols_set = set(cols)
                if cols_set.intersection(THREAD_REF_COL_NAMES):
                    unknown_with_refs.append(tname)

        missing_requirements: List[str] = []
        if db_key in KNOWN_BASELINE_SCHEMAS:
            for req_table, req_cols in KNOWN_BASELINE_SCHEMAS[db_key].items():
                if req_table in tables_dict:
                    actual_cols = set(tables_dict[req_table])
                    missing_cols = req_cols - actual_cols
                    if missing_cols:
                        missing_requirements.append(f"表 {req_table} 缺少列: {missing_cols}")

        if missing_requirements:
            supported = False
            detail = f"基线结构不匹配: {'; '.join(missing_requirements)}"
        elif unknown_with_refs:
            supported = False
            detail = f"发现包含线程引用的未知表: {unknown_with_refs}，降级为只读"
        else:
            supported = True
            detail = f"结构兼容 (版本: {migration_ver or '无迁移表'}, 表数量: {len(tables_dict)})"

        return DatabaseFingerprint(
            path=db_path,
            exists=True,
            journal_mode=journal_mode,
            migration_version=migration_ver,
            tables=tables_dict,
            unknown_tables_with_thread_ref=tuple(unknown_with_refs),
            supported=supported,
            status_detail=detail,
        )
    finally:
        conn.close()


def get_deletion_topology(db_name: str) -> List[Tuple[str, str]]:
    """返回特定数据库在删除特定 thread_id 时的拓扑依赖时序 (表名, 列名)"""
    db_key = db_name.lower()
    if db_key == "codex-dev.db":
        return [
            ("local_thread_catalog_scan_entries", "thread_id"),
            ("thread_timeline_ledger", "thread_id"),
            ("inbox_items", "thread_id"),
            ("automation_runs", "thread_id"),
            ("automations", "target_thread_id"),
            ("local_thread_catalog", "thread_id"),
        ]
    elif db_key == "thread_history_1.sqlite":
        return [
            ("thread_items", "thread_id"),
            ("thread_turns", "thread_id"),
            ("thread_realtime_items", "thread_id"),
            ("thread_history_projection_state", "thread_id"),
        ]
    elif db_key == "state_5.sqlite":
        return [
            ("thread_artifacts", "thread_id"),
            ("thread_dynamic_tools", "thread_id"),
            ("thread_spawn_edges", "thread_id"),
            ("threads", "id"),
        ]
    elif db_key == "logs_2.sqlite":
        return [
            ("logs", "thread_id"),
        ]
    return []


# ===========================================================================
# 2. Windows API 与进程守门 (Process Guard)
# ===========================================================================

def running_codex_processes() -> List[Dict[str, str]]:
    """使用 Toolhelp32Snapshot 枚举与 Codex/ChatGPT 桌面端相关的活动进程"""
    if os.name != "nt":
        return []

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return []

    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(ProcessEntry32W)
    found: List[Dict[str, str]] = []
    try:
        has_entry = bool(process_first(snapshot, ctypes.byref(entry)))
        while has_entry:
            img = entry.szExeFile
            img_cf = img.casefold()
            if "codex" in img_cf or "chatgpt" in img_cf:
                if "clean" not in img_cf and "python" not in img_cf:
                    found.append({"image_name": img, "pid": str(entry.th32ProcessID)})
            has_entry = bool(process_next(snapshot, ctypes.byref(entry)))
    finally:
        close_handle(snapshot)
    return found


def check_exclusive_file_access(path: Path) -> bool:
    """使用 CreateFileW 尝试以排他方式打开文件，验证是否存在锁占用"""
    if not path.exists():
        return True
    if os.name != "nt":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    # GENERIC_READ | GENERIC_WRITE = 0xC0000000, dwShareMode = 0 (排他)
    handle = create_file(str(path.resolve()), 0xC0000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    close_handle(handle)
    return True


class SingleInstanceMutex:
    """清理工具单实例互斥锁，杜绝多实例并发写入"""

    def __init__(self, name: str = "Global\\CodexUnifiedCleanupTool_LZH_Mutex") -> None:
        self.name = name
        self.handle: Optional[int] = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, ctypes.c_wchar_p]
        create_mutex.restype = wintypes.HANDLE
        self.handle = create_mutex(None, True, self.name)
        last_err = ctypes.get_last_error()
        if last_err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return bool(self.handle)

    def release(self) -> None:
        if self.handle and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None


def checkpoint_wal_database(path: Path) -> Tuple[bool, int, int, int]:
    """对 WAL 数据库执行受检 checkpoint，读取 (busy, log, checkpointed)"""
    if not path.exists():
        return (True, 0, 0, 0)
    try:
        conn = sqlite3.connect(str(path.resolve()), timeout=15)
        conn.execute("PRAGMA busy_timeout = 15000")
        row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        conn.close()
        if row:
            busy, log, ckpt = int(row[0]), int(row[1]), int(row[2])
            return (busy == 0, busy, log, ckpt)
        return (True, 0, 0, 0)
    except Exception:
        return (False, -1, -1, -1)


# ===========================================================================
# 3. 路径安全与卷解析
# ===========================================================================

def normalize_path(path_str: str) -> str:
    """规范化 Windows 路径：处理 \\?\\ 前缀、正斜杠化与小写化"""
    p = str(path_str).strip()
    if p.startswith("\\\\?\\"):
        p = p[4:]
    p = p.replace("\\", "/").rstrip("/")
    return p.lower()


def get_actual_volume(path: Path) -> str:
    """获取指定路径实际所在的物理磁盘卷标识 (如 'C:', 'D:')"""
    resolved = path.resolve(strict=False)
    drive, _ = os.path.splitdrive(str(resolved))
    return drive.upper()


# ===========================================================================
# 4. 会话分类与多源证据模型
# ===========================================================================

class SessionState(Enum):
    CLOUD_READONLY = "CLOUD_READONLY"
    IN_USE = "IN_USE"
    HEALTHY_LOCAL = "HEALTHY_LOCAL"
    CATALOG_LAG = "CATALOG_LAG"
    VERIFIED_GHOST = "VERIFIED_GHOST"
    BROKEN_BODY = "BROKEN_BODY"
    ORPHAN_CANDIDATE = "ORPHAN_CANDIDATE"
    UNKNOWN_CONFLICT = "UNKNOWN_CONFLICT"


@dataclass
class SessionItem:
    thread_id: str
    host_id: str
    display_title: str
    cwd: str
    project_display: str
    state: SessionState
    state_reason: str
    rollout_path: Optional[str] = None
    rollout_exists: bool = False
    has_catalog_entry: bool = False
    has_state_entry: bool = False
    has_history_entry: bool = False
    has_session_index_entry: bool = False
    has_lock_file: bool = False
    missing_candidate: int = 0
    updated_at_text: str = ""
    target_fingerprint: str = ""


class SessionScanner:
    """全量会话多源证据扫描与分类器"""

    def __init__(self, inventory: StorageInventory) -> None:
        self.inventory = inventory

    def scan_all(self) -> Tuple[str, List[SessionItem], List[str]]:
        scan_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S_") + hashlib.md5(os.urandom(8)).hexdigest()[:8].upper()
        warnings: List[str] = []
        all_thread_ids: Set[str] = set()

        # 1. 扫描 codex-dev.db (local_thread_catalog)
        catalog_rows: Dict[str, Dict[str, Any]] = {}
        if self.inventory.codex_dev and self.inventory.codex_dev.exists():
            try:
                conn = connect_readonly(self.inventory.codex_dev)
                rows = conn.execute(
                    "SELECT host_id, thread_id, display_title, cwd, missing_candidate, source_updated_at "
                    "FROM local_thread_catalog"
                ).fetchall()
                for r in rows:
                    tid = str(r["thread_id"]).strip()
                    catalog_rows[tid] = {
                        "host_id": str(r["host_id"]),
                        "title": str(r["display_title"] or "未命名会话"),
                        "cwd": str(r["cwd"] or ""),
                        "missing_candidate": int(r["missing_candidate"] or 0),
                        "updated_at": float(r["source_updated_at"] or 0),
                    }
                    all_thread_ids.add(tid)
                conn.close()
            except sqlite3.OperationalError as e:
                warnings.append(f"codex-dev.db 读取遇到锁或超时: {e}")

        # 2. 扫描 state_5.sqlite
        state_rows: Dict[str, Dict[str, Any]] = {}
        state_db = self.inventory.state_5_primary or self.inventory.state_5_compat
        if state_db and state_db.exists():
            try:
                conn = connect_readonly(state_db)
                rows = conn.execute("SELECT id, rollout_path, cwd, title, updated_at FROM threads").fetchall()
                for r in rows:
                    tid = str(r["id"]).strip()
                    state_rows[tid] = {
                        "rollout_path": str(r["rollout_path"] or ""),
                        "cwd": str(r["cwd"] or ""),
                        "title": str(r["title"] or "未命名会话"),
                        "updated_at": int(r["updated_at"] or 0),
                    }
                    all_thread_ids.add(tid)
                conn.close()
            except sqlite3.OperationalError as e:
                warnings.append(f"state_5.sqlite 读取遇到锁或超时: {e}")

        # 3. 扫描 thread_history_1.sqlite
        history_thread_ids: Set[str] = set()
        if self.inventory.thread_history_1 and self.inventory.thread_history_1.exists():
            try:
                conn = connect_readonly(self.inventory.thread_history_1)
                for table in ("thread_turns", "thread_items", "thread_history_projection_state"):
                    rows = conn.execute(f"SELECT DISTINCT thread_id FROM {table}").fetchall()
                    for r in rows:
                        if r[0]:
                            history_thread_ids.add(str(r[0]).strip())
                conn.close()
            except sqlite3.OperationalError as e:
                warnings.append(f"thread_history_1.sqlite 读取遇到锁或超时: {e}")

        # 4. 扫描 session_index.jsonl
        session_index_ids: Set[str] = set()
        if self.inventory.session_index_jsonl and self.inventory.session_index_jsonl.exists():
            try:
                with open(self.inventory.session_index_jsonl, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                obj = json.loads(line)
                                if "id" in obj:
                                    session_index_ids.add(str(obj["id"]).strip())
                            except Exception:
                                pass
            except Exception as e:
                warnings.append(f"session_index.jsonl 解析提示: {e}")

        # 5. 扫描 thread-writer-locks
        locked_threads: Set[str] = set()
        if self.inventory.writer_locks_dir and self.inventory.writer_locks_dir.exists():
            for p in self.inventory.writer_locks_dir.glob("*.lock"):
                locked_threads.add(p.stem)

        # 6. 多源证据综合分类
        items: List[SessionItem] = []
        for tid in sorted(all_thread_ids):
            cat = catalog_rows.get(tid)
            st = state_rows.get(tid)
            has_cat = cat is not None
            has_st = st is not None
            has_hist = tid in history_thread_ids
            has_idx = tid in session_index_ids
            has_lock = tid in locked_threads

            host_id = cat["host_id"] if cat else "local"
            title = (cat["title"] if cat else (st["title"] if st else "未知会话"))
            cwd = (cat["cwd"] if cat and cat["cwd"] else (st["cwd"] if st and st["cwd"] else ""))
            missing_cand = cat["missing_candidate"] if cat else 0

            raw_rollout = st["rollout_path"] if st else ""
            rollout_path_obj: Optional[Path] = None
            rollout_exists = False
            if raw_rollout:
                clean_rp = raw_rollout[4:] if raw_rollout.startswith("\\\\?\\") else raw_rollout
                rollout_path_obj = Path(clean_rp)
                rollout_exists = rollout_path_obj.exists() and rollout_path_obj.is_file()

            if host_id != "local" or host_id.startswith("chatgpt:"):
                state = SessionState.CLOUD_READONLY
                reason = f"云端账户同步会话 (host_id: {host_id[:16]}...)"
            elif has_lock:
                state = SessionState.IN_USE
                reason = "正被 Codex 写入锁锁定中"
            elif has_cat and not has_st and not has_hist and not has_idx and not rollout_exists:
                state = SessionState.VERIFIED_GHOST
                reason = "正文与核心状态已完全清空，仅侧边栏目录残留"
            elif has_st and not rollout_exists:
                state = SessionState.BROKEN_BODY
                reason = "数据库保留状态，但磁盘上的 rollout 物理文件已丢失"
            elif not has_cat and (has_st or rollout_exists):
                state = SessionState.CATALOG_LAG
                reason = "本地状态或物理文件存在，但侧边栏目录暂未注册"
            elif has_st and rollout_exists:
                state = SessionState.HEALTHY_LOCAL
                reason = "元数据完整且物理 rollout 正常可读"
            else:
                state = SessionState.UNKNOWN_CONFLICT
                reason = "多源证据冲突或处于中间同步状态"

            if cwd:
                proj_name = Path(cwd).name or cwd
                proj_display = f"{proj_name} ({cwd})"
            else:
                proj_display = "未绑定具体本地目录"

            fp_src = f"{tid}|{host_id}|{title}|{cwd}|{state.value}|{int(rollout_exists)}"
            target_fp = hashlib.md5(fp_src.encode("utf-8")).hexdigest()[:8].upper()

            items.append(
                SessionItem(
                    thread_id=tid,
                    host_id=host_id,
                    display_title=title,
                    cwd=cwd,
                    project_display=proj_display,
                    state=state,
                    state_reason=reason,
                    rollout_path=str(rollout_path_obj) if rollout_path_obj else None,
                    rollout_exists=rollout_exists,
                    has_catalog_entry=has_cat,
                    has_state_entry=has_st,
                    has_history_entry=has_hist,
                    has_session_index_entry=has_idx,
                    has_lock_file=has_lock,
                    missing_candidate=missing_cand,
                    target_fingerprint=target_fp,
                )
            )

        return (scan_id, items, warnings)


# ===========================================================================
# 5. 备份管理器 (基于 sqlite3.Connection.backup)
# ===========================================================================

class BackupManager:
    """基于 Connection.backup() 与 SHA-256 校验的快照冷备份管理器"""

    def __init__(self, operation_id: str, custom_backup_dir: Optional[Path] = None) -> None:
        self.operation_id = operation_id
        if custom_backup_dir:
            self.base_dir = custom_backup_dir
        else:
            local_appdata = os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            self.base_dir = Path(local_appdata) / "CodexCleanup" / "backups" / operation_id

    def compute_sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def backup_database(self, source_path: Path) -> Dict[str, Any]:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        target_name = source_path.name
        target_path = self.base_dir / target_name

        if not source_path.exists():
            return {"source": str(source_path), "status": "skipped_not_exists"}

        src_conn = sqlite3.connect(str(source_path.resolve()), timeout=15)
        tgt_conn = sqlite3.connect(str(target_path.resolve()), timeout=15)
        try:
            src_conn.backup(tgt_conn)
        finally:
            tgt_conn.close()
            src_conn.close()

        check_conn = sqlite3.connect(str(target_path.resolve()))
        try:
            qc = check_conn.execute("PRAGMA quick_check").fetchone()
            quick_check_res = str(qc[0]) if qc else "unknown"
        finally:
            check_conn.close()

        if quick_check_res != "ok":
            raise RuntimeError(f"备份快照 quick_check 校验失败: {target_path} (结果: {quick_check_res})")

        sha256 = self.compute_sha256(target_path)
        return {
            "source": str(source_path),
            "backup_target": str(target_path),
            "quick_check": quick_check_res,
            "sha256": sha256,
            "status": "backed_up",
        }

    def backup_plain_file(self, source_path: Path) -> Dict[str, Any]:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not source_path.exists():
            return {"source": str(source_path), "status": "skipped_not_exists"}

        target_path = self.base_dir / source_path.name
        shutil.copy2(source_path, target_path)
        sha256 = self.compute_sha256(target_path)
        return {
            "source": str(source_path),
            "backup_target": str(target_path),
            "sha256": sha256,
            "status": "backed_up",
        }


# ===========================================================================
# 6. 同物理卷原子隔离管理器
# ===========================================================================

class QuarantineManager:
    """同卷自适应物理文件隔离区管理器 (严禁跨卷复制)"""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self.quarantine_roots: Dict[str, Path] = {}

    def get_quarantine_dir_for_volume(self, source_path: Path) -> Path:
        vol = get_actual_volume(source_path)
        if vol not in self.quarantine_roots:
            q_dir = Path(f"{vol}\\.codex_quarantine_{self.operation_id}")
            q_dir.mkdir(parents=True, exist_ok=True)
            self.quarantine_roots[vol] = q_dir
        return self.quarantine_roots[vol]

    def quarantine_file(self, source_path: Path) -> Optional[Path]:
        if not source_path.exists():
            return None
        q_dir = self.get_quarantine_dir_for_volume(source_path)
        target_path = q_dir / source_path.name
        os.replace(source_path, target_path)
        return target_path


# ===========================================================================
# 7. 级联清理与执行引擎
# ===========================================================================

class ExecutionEngine:
    """事务级分库级联删除与完整性后置校验引擎"""

    def __init__(self, inventory: StorageInventory) -> None:
        self.inventory = inventory

    def execute_plan(
        self,
        target_items: List[SessionItem],
        backup_mgr: BackupManager,
        quarantine_mgr: QuarantineManager,
    ) -> Dict[str, Any]:
        tids = [item.thread_id for item in target_items]
        op_id = backup_mgr.operation_id

        report: Dict[str, Any] = {
            "operation_id": op_id,
            "time": dt.datetime.now().astimezone().isoformat(),
            "target_thread_count": len(tids),
            "target_thread_ids": tids,
            "backups": [],
            "quarantined_files": [],
            "database_results": {},
            "status": "in_progress",
        }

        # 1. 建立完整冷备份快照
        dbs_to_backup = [
            self.inventory.codex_dev,
            self.inventory.state_5_primary,
            self.inventory.state_5_compat,
            self.inventory.thread_history_1,
            self.inventory.logs_2_primary,
            self.inventory.logs_2_compat,
        ]
        for db in dbs_to_backup:
            if db and db.exists():
                res = backup_mgr.backup_database(db)
                report["backups"].append(res)

        if self.inventory.global_state_json and self.inventory.global_state_json.exists():
            report["backups"].append(backup_mgr.backup_plain_file(self.inventory.global_state_json))
        if self.inventory.session_index_jsonl and self.inventory.session_index_jsonl.exists():
            report["backups"].append(backup_mgr.backup_plain_file(self.inventory.session_index_jsonl))

        manifest_path = backup_mgr.base_dir / "manifest_sha256.json"
        manifest_path.write_text(json.dumps(report["backups"], indent=2, ensure_ascii=False), encoding="utf-8")

        # 2. 分库执行事务删除
        marks = ",".join("?" for _ in tids)
        for db_name, db_path in [
            ("codex-dev.db", self.inventory.codex_dev),
            ("state_5.sqlite (primary)", self.inventory.state_5_primary),
            ("state_5.sqlite (compat)", self.inventory.state_5_compat),
            ("thread_history_1.sqlite", self.inventory.thread_history_1),
            ("logs_2.sqlite (primary)", self.inventory.logs_2_primary),
            ("logs_2.sqlite (compat)", self.inventory.logs_2_compat),
        ]:
            if not db_path or not db_path.exists():
                continue

            topology = get_deletion_topology(db_path.name)
            conn = sqlite3.connect(str(db_path.resolve()), timeout=30)
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA foreign_keys = ON")
            deleted_rows: Dict[str, int] = {}
            try:
                with conn:
                    for table_name, col_name in topology:
                        has_tbl = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
                        ).fetchone()
                        if has_tbl:
                            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
                            if col_name in cols:
                                cur = conn.execute(
                                    f'DELETE FROM "{table_name}" WHERE "{col_name}" IN ({marks})', tids
                                )
                                deleted_rows[table_name] = cur.rowcount

                fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_violations:
                    raise RuntimeError(f"数据库 {db_path.name} 清理后触发外键冲突: {fk_violations}")
                qc = conn.execute("PRAGMA quick_check").fetchone()
                if qc and qc[0] != "ok":
                    raise RuntimeError(f"数据库 {db_path.name} 清理后 quick_check 校验失败: {qc[0]}")

                report["database_results"][db_name] = {"status": "success", "rows": deleted_rows}
            finally:
                conn.close()

        # 3. 修剪 session_index.jsonl
        if self.inventory.session_index_jsonl and self.inventory.session_index_jsonl.exists():
            src_idx = self.inventory.session_index_jsonl
            tmp_idx = src_idx.with_suffix(".tmp")
            tids_set = set(tids)
            retained_count = 0
            removed_count = 0
            with open(src_idx, "r", encoding="utf-8", errors="replace") as f_in, \
                 open(tmp_idx, "w", encoding="utf-8", newline="\n") as f_out:
                for line in f_in:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    try:
                        obj = json.loads(line_s)
                        if str(obj.get("id")) in tids_set:
                            removed_count += 1
                            continue
                    except Exception:
                        pass
                    f_out.write(line_s + "\n")
                    retained_count += 1
            os.replace(tmp_idx, src_idx)
            report["session_index_result"] = {"removed": removed_count, "retained": retained_count}

        # 4. 修剪 .codex-global-state.json
        if self.inventory.global_state_json and self.inventory.global_state_json.exists():
            gs_path = self.inventory.global_state_json
            try:
                gs_data = json.loads(gs_path.read_text(encoding="utf-8"))
                removed_client_ids = 0
                if "client_ids" in gs_data and isinstance(gs_data["client_ids"], dict):
                    for tid in tids:
                        if tid in gs_data["client_ids"]:
                            del gs_data["client_ids"][tid]
                            removed_client_ids += 1

                tmp_gs = gs_path.with_suffix(".tmp")
                tmp_gs.write_text(json.dumps(gs_data, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp_gs, gs_path)
                report["global_state_result"] = {"removed_client_ids": removed_client_ids}
            except Exception as e:
                report["global_state_result"] = {"status": "error", "error": str(e)}

        # 5. 移入同卷隔离区
        for item in target_items:
            if item.rollout_path and Path(item.rollout_path).exists():
                q_path = quarantine_mgr.quarantine_file(Path(item.rollout_path))
                if q_path:
                    report["quarantined_files"].append({"source": item.rollout_path, "quarantine": str(q_path)})

            if self.inventory.writer_locks_dir:
                lock_file = self.inventory.writer_locks_dir / f"{item.thread_id}.lock"
                if lock_file.exists():
                    q_lock = quarantine_mgr.quarantine_file(lock_file)
                    if q_lock:
                        report["quarantined_files"].append({"source": str(lock_file), "quarantine": str(q_lock)})

        report["status"] = "COMPLETED"
        return report
