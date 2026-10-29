@echo off
chcp 936 >nul
cd /d "%~dp0"
title 场馆抢单助手

rem 第一次双击会自动准备环境，之后每次都是秒开。

if exist ".venv\Scripts\python.exe" goto run

echo.
echo   ============================================
echo     第一次使用，正在为你准备运行环境
echo     大约 1~3 分钟，只需准备这一次
echo   ============================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_env.ps1"
if errorlevel 1 goto setupfail
if not exist ".venv\Scripts\python.exe" goto setupfail

echo.
echo   环境就绪，正在启动...
echo.

:run
if not exist ".venv\Scripts\pythonw.exe" goto rundebug
start "" ".venv\Scripts\pythonw.exe" run.py

rem 服务可能要几秒才起来，多等一会儿再判断，
rem 否则会误判成「启动失败」，再拉一次 run.py，弹出第二个窗口
set /a TRIES=0
:waitport
netstat -an | findstr ":8788" | findstr "LISTENING" >nul
if not errorlevel 1 goto launched
set /a TRIES+=1
if %TRIES% GEQ 25 goto rundebug
ping -n 2 127.0.0.1 >nul
goto waitport

:launched
exit /b 0

:rundebug
echo 程序没有正常启动，下面用调试方式再跑一次，把报错内容发给我：
echo.
".venv\Scripts\python.exe" run.py
pause
exit /b 0

:setupfail
echo.
echo   环境准备没成功，上面那几行是具体原因。
echo.
echo   最常见的两个原因：
echo     1. 网络不通，下载不到 Python 或依赖
echo     2. 目录没有写权限，换个位置解压再试
echo.
echo   也可以手动装 Python 3.10 以上（python.org，勾选 Add python.exe to PATH），
echo   装完重新双击本文件。
echo.
pause
exit /b 1
