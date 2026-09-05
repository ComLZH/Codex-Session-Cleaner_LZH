<div align="center">

# Codex-Session-Cleaner_LZH

<p>
  <a href="README.md">简体中文</a> | <b>English</b>
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6.svg)
![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-brightgreen.svg)
![Author: LZH](https://img.shields.io/badge/Author-LZH-orange.svg)

<p>
  <b>Choose which conversations to keep. Manually clean up local records you no longer need.</b>
  <br />
  A Windows tool for managing local Codex sessions: browse by project, delete selected sessions, and clean up leftover sidebar entries.
</p>

</div>

---

## 📖 Why I built this

This project started with a simple need while using Codex: **I wanted to manually delete some conversations I no longer needed and manage the session records stored on my computer.**

Over time, conversations accumulated across different projects. Some were worth keeping; others were temporary questions, repeated attempts, or work that was already finished. I wanted to choose what stayed and what went.

When I started building this tool, archiving in the Codex client did not meet that need: the records remained on my computer after archiving, and I could not find a direct way to clean up those local records.

That is what this project aims to offer others with the same need: **inspect local sessions, explicitly select what to remove, and run the cleanup after reviewing the selection.**

Working on deletion also exposed a related problem. Removing only the conversation file could leave titles in databases and indices, producing sidebar “ghost entries” that no longer opened. The tool therefore also handles related records and ghost entry cleanup.

> **What does “delete” mean here?** The tool removes selected session records from supported local databases and indices, and moves the corresponding conversation files into quarantine. Backups and quarantined files remain on your computer. This is not secure erasure and does not delete copies on servers or other devices.

## ✨ What can you do with it?

| Need | Feature |
| :--- | :--- |
| See conversations across projects | Scan identifiable sessions in the current user's default `.codex` data root and group them by working directory |
| Manually delete one or more unwanted conversations | Enter a session number, a list, or a range, such as `1`, `1,3`, or `2-5` |
| Target a specific session | Enter its full Thread ID to select it from the current scan |
| Check your selection | Inspect titles, working directories, record sources, file paths, and status; review a plan and enter a confirmation phrase before deletion |
| Remove titles left behind after conversation files were deleted | Select entries classified as local ghosts and confirm their cleanup |
| Investigate local storage issues | View session evidence summaries and run read-only SQLite health checks |

Operations apply to **whole sessions**. Deleting individual messages within a conversation is not supported. Project grouping helps you browse and select sessions; it does not mean deleting project source code or removing every piece of project data in one operation.

## 🖥️ Quick start

### 1. Download and launch

Download and extract the repository ZIP, or clone the repository. In the project root, double-click:

**`启动Codex清理工具.cmd`**

Running from source requires **Windows and Python 3.10+**, with no third-party Python packages. The launcher first tries Python in the current user's Codex runtime cache, then other configured interpreters. If no usable interpreter is available, install Python and add it to PATH.

The console opens with a scan overview and an operation menu. Enter `2` to view sessions grouped by project. Read-only browsing is available while Codex is running.

### 2. Select conversations to delete

1. Review session titles and working directories. Enter `3` to inspect an evidence summary if needed.
2. Save your work and fully exit Codex, including any remaining tray or background processes.
3. Enter `1` in the tool to refresh the scan, then `5` to view the list and select sessions.
4. Enter numbers from the current list, such as `1,3` or `2-5`. Full-width digits and common Chinese separators are also accepted.
5. Review the target sessions, expected scope, and backup location in the dry-run preview.
6. Enter the confirmation phrase with the operation code exactly as shown, for example `确认删除2个本机会话 A7C19E2B`. Use the phrase displayed for your operation; press Enter without typing anything to cancel.
7. After completion, check the backup location and audit report. Reopen Codex to verify both the removed sessions and the sessions you wanted to keep.

For example, a project might have three conversations numbered `1` (main development), `2` (temporary test), and `3` (duplicate attempt). To clean up the last two, enter `2,3` in menu `5`, then check their titles and Thread IDs in the preview.

### 3. Other menu options

This is a functional summary; the numbers match the current program:

| Number | Action |
| :---: | :--- |
| `1` | Rescan the current data root |
| `2` | View sessions grouped by project |
| `3` | Inspect a session evidence summary |
| `4` | Select all entries classified as ghosts for cleanup preview and confirmation |
| `5` | Select one or more sessions by number for deletion |
| `6` | Select a session by full Thread ID for deletion |
| `7` | Run read-only SQLite health checks |
| `8` | Run optional SQLite space maintenance (VACUUM) |
| `0` | Exit |

The current console interface and deletion confirmation phrases are in Chinese. This English README explains the corresponding operations.

## 🔍 Why clean up databases and indices too?

In the local storage layouts investigated for this project, a session can have records in several places. Removing its `rollout-*.jsonl` conversation file alone may leave related records behind.

| Location (relative to `.codex`) | Related content |
| :--- | :--- |
| Rollout files under `sessions/` | Conversation records; execution uses the file path supplied by the state database |
| `sqlite/codex-dev.db` | Sidebar catalog, titles, and other session references |
| `state_5.sqlite`, `sqlite/state_5.sqlite` | Session state, titles, and rollout paths |
| `thread_history_1.sqlite` | Turns, message items, and history projections |
| `logs_2.sqlite`, `sqlite/logs_2.sqlite` | Logs associated with a Thread ID |
| `session_index.jsonl`, `.codex-global-state.json` | Session index and the `client_ids` mapping handled by the current implementation |

The tool uses selected Thread IDs to clean up related records covered by its implemented adapters. **Ghost cleanup is one part of this local session management workflow**; normal local sessions that still open can also be selected for deletion.

Storage layouts may change with Codex versions. These paths and cleanup rules do not represent complete coverage of every version or every location that might contain residual data.

## 🛡️ Confirmation, backups, and scope

The current implementation includes process checks before deletion, access and WAL checks for some databases, a mutex for cleanup writes, and a confirmation phrase containing an operation code. The executor creates SQLite backups and a SHA-256 manifest, commits cleanup transactions separately for each database, and runs foreign key and integrity checks.

Before use, understand these boundaries:

- **Local scope:** The default data root is `%USERPROFILE%\.codex` for the current Windows user. Sessions identified as non-local are displayed read-only; a selection containing them is blocked from deletion. The tool does not perform server-side or cross-device deletion.
- **Cleanup targets:** The intended targets are session records and related data, excluding project source code. The executor relies on rollout paths from the database, and the full planned path boundary protections are not yet implemented.
- **Backups and quarantine:** Database and text backups go to `%LOCALAPPDATA%\CodexCleanup\backups\<operation_id>\`. Conversation files move to `.codex_quarantine_<operation_id>` at the root of their source volume. Completion reports are written to `reports/` in the tool directory and include actual paths. Backups may contain data from unselected sessions, and quarantined files still contain conversation content.
- **Recovery:** There is no one-click restore command or automatic rollback across databases in the current version. If a step fails, manual recovery from backups and quarantine may be necessary. Database and file operations do not form a single atomic transaction.
- **Compatibility and validation:** Schema diagnostics are not a complete compatibility gate for writes. Current tests do not cover the full deletion and recovery flows or every Codex version. This tool does not promise to remove every trace of a conversation.

## 📁 Repository structure and technical notes

```text
Codex-Session-Cleaner_LZH/
├── 启动Codex清理工具.cmd       # Windows double-click launcher
├── run_console.ps1             # Python interpreter discovery and launch
├── codex_engine.py             # Scanning, classification, backups, quarantine, cleanup
├── codex_console.py            # Menus, selection, preview, confirmation
├── README.md                   # Chinese documentation
├── README_en.md                # English documentation
├── LICENSE                     # MIT license
├── docs/                       # Investigation notes, instructions, and plans
└── tests/
    └── test_unified_tool.py     # Basic tests and read-only tests against local data
```

Further reading (in Chinese): [storage investigation and deletion approach](docs/01_调查发现与删除原理.md), [manual workflow](docs/02_手动操作流程.md), [safety boundaries and troubleshooting](docs/03_安全边界与故障排查.md), [review of the earlier automated approach](docs/04_旧自动方案复盘.md), [ghost entry cleanup](docs/05_侧边栏幽灵记录修复_20260830.md), and [console integration and generalization plan](docs/06_控制台集成与通用化清理方案规划.md).

The `docs/` directory includes historical descriptions and plans that are not fully implemented. Some statements differ from the current code. Read them alongside the source and the limits in this README; planned recovery, compatibility, and safety mechanisms should not be treated as completed features.

## 🧪 Running tests

From the project root, run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Tests cover input parsing, path handling, and read-only scans of the current user's actual `.codex` data. Some tests require local databases to exist, and the scan test prints a few session title and ID samples to the terminal. Passing these tests does not fully validate deletion or recovery.

## 📄 License

Developed by **LZH** and released under the [MIT License](LICENSE), provided “as is” under its terms. Feedback, compatibility reports, and improvement suggestions are welcome through Issues.
