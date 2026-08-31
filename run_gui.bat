@echo off
chcp 65001 >nul
title Ren'Py 自动汉化工具
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)

python -c "import win32more" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行需要安装 WinUI 3 框架，正在安装 win32more...
    pip install win32more
    if errorlevel 1 (
        echo [错误] win32more 安装失败，请手动执行: pip install win32more
        pause
        exit /b 1
    )
)

echo 正在启动 Ren'Py 自动汉化工具...
python main.py --gui
