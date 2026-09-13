@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

echo ============================================
echo   语音控件坐标标定
echo ============================================
echo.
echo 默认标定设置里当前选中的目标。
echo 也可以带参数指定：
echo     tools\校准坐标.bat --target wechat
echo     tools\校准坐标.bat --target qq
echo.

where python >nul 2>nul
if errorlevel 1 goto no_python

python "tools\calibrate_target.py" %*
echo.
pause
exit /b 0

:no_python
echo [ERROR] 找不到 Python。请安装 Python 3.10 或更高版本：https://www.python.org/
echo.
pause
exit /b 1
