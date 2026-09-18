@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   一键检查：纯逻辑单测 + 仓库自检 + 合成自检
echo ============================================
echo.
echo 这三项不需要窗口、麦克风、虚拟声卡，也不需要真实 API Key。
echo 需要这些东西的检查请单独跑：tools\calibrate_target.py、
echo tools\measure_latency.py、tools\check_default_devices.py。
echo.

where python >nul 2>nul
if errorlevel 1 goto no_python

echo === tests\test_units.py（纯函数单测） ===
python "tests\test_units.py" || goto failed
echo.
echo === tools\check_repo.py（行尾 / 编译 / 目标接口） ===
python "tools\check_repo.py" || goto failed
echo.
echo === tools\test_translation.py（翻译请求构造，本地 mock） ===
python "tools\test_translation.py" || goto failed
echo.
echo === synth\synthesize.mjs --check（三条合成路径） ===
where node >nul 2>nul
if errorlevel 1 (
  echo [跳过] 没找到 Node.js，合成自检需要它。
) else (
  node "synth\synthesize.mjs" --check || goto failed
)
echo.
echo ============================================
echo   全部通过。
echo ============================================
pause
exit /b 0

:no_python
echo [ERROR] 找不到 Python。请安装 Python 3.10 或更高版本：https://www.python.org/
echo.
pause
exit /b 1

:failed
echo.
echo ============================================
echo   有检查没通过，见上面的输出。
echo ============================================
pause
exit /b 1
