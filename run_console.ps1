# run_console.ps1 - Codex 会话管理与清理工具启动器

$ErrorActionPreference = 'Stop'
$toolRoot = $PSScriptRoot
$scriptPath = Join-Path $toolRoot 'codex_console.py'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Program file not found: $scriptPath"
}

$pythonExe = $null
$pythonArgs = @()

$bundledPy = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$condaPy = 'D:\Anaconda\anaconda3\envs\OpenAI_Only_Env\python.exe'
$pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
$pyCmd = Get-Command 'python.exe' -ErrorAction SilentlyContinue

# 依次探测可用的解释器
if (Test-Path -LiteralPath $bundledPy) {
    $pythonExe = $bundledPy
} elseif (Test-Path -LiteralPath $condaPy) {
    $pythonExe = $condaPy
} elseif ($null -ne $pyCmd) {
    $pythonExe = $pyCmd.Source
} elseif ($null -ne $pyLauncher) {
    $pythonExe = $pyLauncher.Source
    $pythonArgs = @('-3')
} else {
    Write-Host "错误: 未能在系统中找到满足要求 (Python 3.10+ 且含标准库) 的 Python 解释器。" -ForegroundColor Red
    Write-Host "请安装 Python 3.10+，或确保其位于 PATH 环境变量中。" -ForegroundColor Yellow
    exit 1
}

& $pythonExe @pythonArgs -B $scriptPath @args
exit $LASTEXITCODE
