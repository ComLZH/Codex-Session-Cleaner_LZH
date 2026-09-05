<div align="center">

# Codex-Session-Cleaner_LZH

<p align="center">
  <a href="README.md">简体中文</a> | <b>English</b>
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](https://microsoft.com)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-brightgreen.svg)](https://python.org)
[![Author: LZH](https://img.shields.io/badge/Author-LZH-orange.svg)]()

<p align="center">
  A professional-grade, multi-project session management and safe cleanup tool for the OpenAI Codex desktop client.
  <br />
  Specifically engineered to resolve persistent sidebar "ghost entries" and multi-source SQLite database desynchronization.
</p>

</div>

---

## 📖 The Problem: Why Do Sidebar Ghost Entries Persist After Deleting Files?

Many developers attempt to clean up old Codex project sessions by manually deleting the corresponding `rollout-*.jsonl` files under the `.codex/sessions/` directory.

However, after restarting the Codex desktop client, **the old session titles still stubbornly appear in the left sidebar**. Clicking on them triggers an error: *"File does not exist"*. These are known as **Sidebar Ghost Entries**.

### Root Cause
Codex desktop storage does not rely on simple flat files. It is an interrelated multi-source architecture:
1. **Desktop UI Catalog Database** (`.codex/sqlite/codex-dev.db`): Houses tables like `local_thread_catalog`, which the frontend sidebar reads directly for metadata caching;
2. **Core State Engine** (`.codex/state_5.sqlite`): Tracks active threads, workspace `cwd`, and `project_id` foreign keys;
3. **Turn History & Projections** (`.codex/thread_history_1.sqlite`): Stores message interaction turns and projection offsets;
4. **Global Text Indices** (`.codex/session_index.jsonl`) and client state (`.codex-global-state.json`).

**Manually deleting rollout files leaves database index references intact.** `Codex-Session-Cleaner_LZH` was created to address this safely, systematically, and completely.

---

## ✨ Key Features

* **🚀 Zero-Configuration Out-of-the-Box**:
  * **No Manual Python Installation Required**: The launcher automatically probes and reuses the official bundled Python 3 runtime shipped with Codex (`%USERPROFILE%\.cache\codex-runtimes\...`). Users can simply double-click and run immediately.
  * **Zero 3rd-Party Dependencies**: The core engine is built strictly using Python standard libraries (`sqlite3`, `ctypes`, `json`, `pathlib`).
* **🔍 Multi-Project Discovery & Mutual-Exclusion Classification**:
  * Automatically scans all local projects and threads under the current user profile;
  * Categorizes each session based on multi-source evidence:
    * `[HEALTHY_LOCAL]`: Metadata and physical rollout files are fully intact;
    * `[VERIFIED_GHOST]`: Only catalog entries persist; rollout and body state are gone (click-to-error items);
    * `[BROKEN_BODY]`: Database tracks the thread, but the physical rollout file is missing;
    * `[CLOUD_READONLY]`: Cloud-synced threads linked to your ChatGPT account (strictly protected, deletion prohibited);
    * `[IN_USE]`: Currently locked or in active communication with a running Codex process.
* **🛡️ Enterprise-Grade Safety Defenses**:
  * **Read/Write Separation**: Safe read-only browsing even while Codex is actively running; exclusive gates only trigger during actual deletion.
  * **Process Guard & Exclusive Locking**: Deeply inspects running `codex.exe` and `ChatGPT.exe` background/tray processes before write operations, verifying Windows kernel file locks.
  * **SQLite Cold Backup Snapshots**: Leverages Python's native `sqlite3.Connection.backup()` to take non-blocking, transaction-safe snapshots with SHA-256 manifests into `%LOCALAPPDATA%\CodexCleanup\backups\`.
  * **Same-Volume Quarantine**: Physical rollout files are atomically moved to a designated same-volume quarantine directory rather than being permanently destroyed immediately.
  * **Interactive Dry-Run & Dynamic Passphrase**: Displays full impact scope and requires exact entry of a generated dynamic security passphrase (e.g., `确认删除3个本机会话 A7C19E2B`) to prevent accidental clicks.
  * **Topological Cascading Deletion & Verification**: Follows foreign key hierarchy across all databases and automatically runs `PRAGMA foreign_key_check` and `PRAGMA quick_check`.

---

## 🖥️ Quick Start

Simply double-click the launcher script in the root directory:
👉 **`启动Codex清理工具.cmd`**

The interactive console dashboard will launch immediately:

```text
================================================================================
       Codex Session Cleaner (Codex-Session-Cleaner_LZH v2.1.1)
================================================================================
[Scope] Current User: User | Root Path: C:\Users\User\.codex
[Process Status] Running (12 processes detected, currently Read-Only mode)
[Schema Fingerprint] codex-dev.db: Compatible | state_5.sqlite: Compatible
[Session Stats] Total: 15 | Healthy: 5 | Ghosts: 0 | Cloud: 7 | Snapshot: 5E75BF8B
--------------------------------------------------------------------------------
📁 Project: Node Rule Engine (1 thread)
  [ 1] [Healthy]      Diagnose residual records                 (ID: 01a05223...)

📁 Project: Legacy Demo Tool (1 thread)
  [ 2] [Ghost Entry]  Old Deleted Session Title                 (ID: 019ece23...)

📁 Project: Unbound / Cloud Synchronized (7 threads)
  [ 3] [Cloud Synced] GPT Image PPT Generation                  (ID: 69ed9daa...)
--------------------------------------------------------------------------------
[Available Operations]
  [1] Re-scan data directory
  [2] View all projects and session trees
  [3] Inspect full underlying evidence for a session
  [4] One-click cleanup all [Sidebar Ghost Entries] (Recommended)
  [5] Interactively select sessions to delete (supports: 1,3 or 2-5)
  [6] Delete specific session by 36-char Thread ID
  [7] Run read-only SQLite quick_check diagnostics
  [8] Optional database vacuum maintenance (reclaim fragmented disk space)
  [0] Exit tool
================================================================================
Enter menu choice: 
```

### Typical Workflows
1. **Audit & Inspection**: Press `2` to view the grouped tree. Press `3` and type an index to inspect underlying database rows and file paths.
2. **Remove Click-to-Error Ghost Entries**: Exit Codex desktop completely (Quit from the system tray), reopen the tool, and press `4`. It automatically isolates and cleans all ghost headers.
3. **Selective Deletion**: Press `5`, enter chosen indices (supports comma-separated list like `1,3` or ranges like `2-4`), review the dry-run plan, and enter the confirmation passphrase to execute safely.

---

## 📁 Repository Structure

```text
Codex-Session-Cleaner_LZH/
├── 启动Codex清理工具.cmd       # Primary Windows double-click launcher (Pure ASCII, encoding-safe)
├── run_console.ps1             # Adaptive PowerShell launcher (Auto-detects bundled Codex runtime)
├── codex_engine.py             # Core Engine (Schema fingerprinting, scanner, snapshots, quarantine, cascading cleanup)
├── codex_console.py            # Console UI (Menu dashboard, NFKC input parser, dry-run, confirmation guard)
├── README.md                   # Simplified Chinese Documentation (Default)
├── README_en.md                # English Documentation
├── LICENSE                     # MIT Open Source License
├── docs/                       # Technical architecture & design documentation
│   ├── 01_调查发现与删除原理.md
│   ├── 02_手动操作流程.md
│   ├── 03_安全边界与故障排查.md
│   ├── 04_旧自动方案复盘.md
│   ├── 05_侧边栏幽灵记录修复_20260830.md
│   └── 06_控制台集成与通用化清理方案规划.md
└── tests/                      # Automated unit testing suite
    └── test_unified_tool.py
```

---

## 🧪 Running Tests

To run the full unit test suite:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## ⚠️ Disclaimer & Safety Boundaries

1. **Strict Local Boundary**: This tool operates exclusively inside the user's `.codex` directory. **It will NEVER modify, delete, or touch your project source code files under any circumstance.**
2. **Local Cleanup Only**: Because OpenAI has not publicly documented a permanent remote deletion API for desktop tasks, this tool only purges local SQLite records, local JSON indices, and local disk rollouts. It cannot certify physical deletion on OpenAI server-side backups.
3. **Automatic Backups**: A complete snapshot of all affected SQLite databases is created under `%LOCALAPPDATA%\CodexCleanup\backups\` before any modification takes place.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
Author: **LZH** (2026)
