# push.ps1 —— 一键提交并推送（提交描述由你自己写）
# 用法:
#   .\push.ps1               运行后按提示输入提交描述
#   .\push.ps1 "你的描述"     直接带描述提交
#   .\push.ps1 -NoPush       只本地提交不推送（自测用）
param(
    [string]$Message = '',
    [switch]$NoPush
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { Write-Host '[错误] 当前目录不是 git 仓库。'; exit 1 }

$st = git status --short
if (-not $st) { Write-Host '[无改动] 没有需要提交的内容。'; exit 0 }
Write-Host '[改动]'
$st | ForEach-Object { Write-Host "  $_" }

git add -A
if ($LASTEXITCODE -ne 0) { Write-Host '[错误] git add 失败。'; exit 1 }

$msg = "$Message".Trim()
$tries = 0
while (-not $msg) {
    if ($tries -ge 3) { Write-Host '[错误] 三次未输入描述，已取消。'; exit 1 }
    $tries++
    $in = Read-Host '请输入提交描述'
    if ($null -eq $in) { Write-Host '[错误] 读不到输入，已取消。'; exit 1 }
    $msg = "$in".Trim()
    if (-not $msg) { Write-Host '[提示] 描述不能为空。' }
}

# -F 走 UTF-8 文件传描述，避免引号/特殊字符被 shell 转义
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("commit_msg_{0}.txt" -f $PID)
[System.IO.File]::WriteAllText($tmp, $msg + "`n", (New-Object System.Text.UTF8Encoding($false)))
git commit -F $tmp
Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0) { Write-Host '[错误] 提交失败。'; exit 1 }

if ($NoPush) { Write-Host '[完成] 已本地提交（-NoPush：未推送）。'; git log --oneline -3; exit 0 }

git push
if ($LASTEXITCODE -ne 0) { Write-Host '[错误] 推送失败（检查网络；github.com 已配置代理）。'; exit 1 }
Write-Host '[完成] 已提交并推送：'
git log --oneline -5
exit 0