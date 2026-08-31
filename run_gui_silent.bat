@echo off
rem 无终端窗口启动方式：使用 pythonw，启动后即可关闭本窗口，GUI 继续运行
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)

python -c "import win32more" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行需要安装 win32more，正在安装...
    pip install win32more
    if errorlevel 1 (
        echo [错误] win32more 安装失败，请手动执行: pip install win32more
        pause
        exit /b 1
    )
)

rem 用 pythonw 无控制台启动 GUI，本终端窗口可随时关闭
start "" pythonw main.py --gui
exit /b 0
