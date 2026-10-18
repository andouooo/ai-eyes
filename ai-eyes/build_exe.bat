@echo off
chcp 65001 >nul
title 竞眼雷达 RivalEye - Windows 一键打包
echo ================================================
echo   竞眼雷达 RivalEye - 一键打包为单文件 exe
echo   （需要已安装 Python 3.10+，双击即可运行）
echo ================================================
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py") else (set "PY=python")

echo.
echo [1/4] 安装依赖（国内镜像）...
%PY% -m pip install -r requirements.txt pyinstaller platformdirs -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto fail

echo.
echo [2/4] 下载 Chromium 内核（只需一次，约 120MB）...
%PY% -m playwright install chromium

echo.
echo [3/4] 打包单文件 exe（约 1-3 分钟，成品约 200MB）...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name RivalEye --icon assets\icon.ico --add-data "demo;demo" --collect-all playwright --hidden-import platformdirs --hidden-import uiautomation --hidden-import comtypes run.py
if errorlevel 1 goto fail

echo.
echo [4/4] 复制到桌面...
copy /y "dist\RivalEye.exe" "%USERPROFILE%\Desktop\RivalEye.exe" >nul
if errorlevel 1 goto fail

echo.
echo ================================================
echo   完成！桌面已生成 RivalEye.exe
echo   双击 = 启动悬浮窗助手（未配 Key 自动进离线演示）
echo   命令行可用：RivalEye.exe demo
echo               RivalEye.exe watch https://竞品官网
echo   快照/报告生成在 exe 同目录，随文件一起走
echo ================================================
pause
exit /b 0

:fail
echo.
echo ❌ 打包失败：请截图本窗口全部文字发给 AI 排查
pause
exit /b 1
