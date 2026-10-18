<#
  把 ai-eyes 装进 DSH 的用户技能目录（自包含：SKILL.md + bin\）。

  用法：
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Dest "D:\somewhere\skills\ai-eyes"

  卸载：删掉目标目录即可（技能不写注册表、不改系统）。
#>
param(
    [string]$Dest = (Join-Path $env:USERPROFILE ".dsh\skills\ai-eyes")
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bin = Join-Path $Dest "bin"

New-Item -ItemType Directory -Force -Path $bin | Out-Null

Copy-Item (Join-Path $root "rival_eye") $bin -Recurse -Force
Copy-Item (Join-Path $root "scripts\verify_evidence.py") $bin -Force
Copy-Item (Join-Path $root "skills\ai-eyes\SKILL.md") $Dest -Force

# 别把编译缓存带进技能目录
Get-ChildItem (Join-Path $bin "rival_eye") -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "已安装到 $Dest"
Write-Host ("  自检： python `"$bin\rival_eye\eyes.py`" doctor --json")
Write-Host ("  审计： python `"$bin\verify_evidence.py`" --answer <你的回答.md>")
