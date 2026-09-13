@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   文字转油库里
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto no_python

where node >nul 2>nul
if errorlevel 1 goto no_node

python -c "import customtkinter, sounddevice, soundfile, numpy, psutil, pyautogui, affine" >nul 2>nul
if errorlevel 1 goto install_python_deps

if not exist "synth\node_modules\aquestalk.js" goto install_node_deps

goto launch

:install_python_deps
echo [提示] Python 依赖不完整，正在安装...
python -m pip install -r requirements.txt
if errorlevel 1 goto python_deps_failed
if not exist "synth\node_modules\aquestalk.js" goto install_node_deps
goto launch

:install_node_deps
echo [提示] 离线合成引擎依赖未安装，正在安装...
pushd synth
call npm install
popd
if not exist "synth\node_modules\aquestalk.js" goto node_deps_failed
goto launch

:launch
echo 正在启动...
rem Redirect all three standard handles: pythonw has no console of its own, and
rem letting it inherit ours would keep this window's output pipe open.
start "" pythonw "app.pyw" >nul 2>nul <nul

rem pythonw has no console, so a startup crash would be invisible. Give the
rem process a moment and confirm it actually survived.
rem (ping, not timeout: timeout refuses to run when stdin is redirected.)
ping -n 5 127.0.0.1 >nul
tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if errorlevel 1 goto launch_failed

echo.
echo 已启动，程序正在系统托盘运行。
echo 如果没看到窗口，请看日志：%USERPROFILE%\.youkuli-chaspeak\widget.log
ping -n 4 127.0.0.1 >nul
exit /b 0

:launch_failed
echo.
echo [错误] 程序启动失败，或启动后立刻退出了。
echo.
echo 日志文件：%USERPROFILE%\.youkuli-chaspeak\widget.log
echo.
echo 想看具体报错，请在本窗口手动执行下面这条命令：
echo     python app.pyw
echo.
pause
exit /b 1

:no_python
echo [错误] 找不到 Python。
echo 请安装 Python 3.10 或更高版本：https://www.python.org/
echo 安装时请勾选 Add python.exe to PATH。
echo.
pause
exit /b 1

:no_node
echo [错误] 找不到 Node.js。
echo 离线合成引擎需要 Node.js 18 或更高版本：https://nodejs.org/
echo.
pause
exit /b 1

:python_deps_failed
echo.
echo [错误] Python 依赖安装失败。
echo 请手动执行：python -m pip install -r requirements.txt
echo.
pause
exit /b 1

:node_deps_failed
echo.
echo [错误] 离线合成引擎依赖安装失败。
echo 请手动执行：cd synth 然后 npm install
echo.
pause
exit /b 1
