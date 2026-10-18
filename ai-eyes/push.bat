@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo   AI 的眼睛 - 推送到 GitHub
echo   仓库: https://github.com/andouooo/-
echo ================================================
echo.
echo 需要一个 Personal Access Token（勾选 repo 权限）。
echo 生成地址: https://github.com/settings/tokens
echo.
set /p TOKEN=粘贴 token 后回车:
if "%TOKEN%"=="" goto fail
echo.
echo 正在推送（会用本地项目覆盖远端 main）...
git push https://andouooo:%TOKEN%@github.com/andouooo/-.git main --force
if errorlevel 1 goto fail
echo.
echo 完成: https://github.com/andouooo/-
pause
exit /b 0

:fail
echo.
echo 推送失败。检查:
echo   1) token 是否勾了 repo 权限
echo   2) 网络是否能打开 github.com
echo   3) 仓库地址是否是 https://github.com/andouooo/-
pause
exit /b 1
