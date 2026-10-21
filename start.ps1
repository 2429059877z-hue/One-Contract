#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('start','stop','status','restart','diagnose')]
    [string]$Action = 'start',
    [string]$DataDir,
    [switch]$ReadOnly,
    [switch]$Foreground,
    [switch]$NoBrowser,
    [int]$IdleTimeoutSeconds = 1800,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$entry = Join-Path $PSScriptRoot 'scripts\open_rule_studio.py'
$agent = Join-Path $PSScriptRoot 'scripts\rule_studio_agent.py'
if (-not (Test-Path -LiteralPath $entry)) { throw '未找到启动程序，请从完整 One-Contract 目录启动。' }

$candidates = @()
if ($Python) { $candidates += @{Exe=$Python; Prefix=@()} }
else {
    $candidates += @{Exe=(Join-Path $PSScriptRoot '.venv\Scripts\python.exe'); Prefix=@()}
    foreach ($name in @('py','python','python3')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command) {
            $prefix = @()
            if ($name -eq 'py') { $prefix = @('-3') }
            $candidates += @{Exe=$command.Source; Prefix=$prefix}
        }
    }
}
$selected = $null
$fallback = $null
foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate.Exe)) { continue }
    $probe = @($candidate.Prefix) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)')
    try { & $candidate.Exe @probe 2>$null } catch { continue }
    if ($LASTEXITCODE -ne 0) { continue }
    if (-not $fallback) { $fallback = $candidate }
    $probe = @($candidate.Prefix) + @('-c', 'import jsonschema, referencing')
    try { & $candidate.Exe @probe 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { $selected = $candidate; break }
}
if (-not $selected -and $Action -eq 'diagnose') { $selected = $fallback }
if (-not $selected) {
    Write-Host '需要 Python 3.10 或更新版本，以及本项目依赖。启动器不会联网安装。'
    Write-Host '推荐准备独立环境（在此目录打开 PowerShell）：'
    Write-Host '  py -3 -m venv .venv'
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r scripts\requirements-rule-studio-dev.txt'
    Write-Host '准备完成后重启；已有解释器可用 -Python 指定完整路径。'
    exit 2
}
$forward = @($selected.Prefix) + @('-u')
if ($Foreground -and $Action -eq 'start') {
    $forward += @($entry, '--idle-timeout-seconds', [string]$IdleTimeoutSeconds)
    if ($NoBrowser) { $forward += '--no-browser' }
} else {
    $forward += @($agent, $Action)
    if ($Action -in @('start','restart')) {
        $forward += @('--idle-seconds', [string]$IdleTimeoutSeconds)
        if (-not $NoBrowser) { $forward += '--open-browser' }
    }
}
if ($Action -in @('start','restart','diagnose')) {
    if ($DataDir) { $forward += @('--data-dir', $DataDir) }
    if ($ReadOnly) { $forward += '--read-only' }
}
& $selected.Exe @forward
exit $LASTEXITCODE
