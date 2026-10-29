@echo off
chcp 936 >nul
cd /d "%~dp0"
title 场馆抢单助手（调试）

if exist ".venv\Scripts\python.exe" goto run

echo   还没有准备运行环境。
echo   先双击一次「启动.bat」，它会自动装好，装完再回来用这个。
echo.
pause
exit /b 1

:run
echo   调试模式：窗口会一直留着，出错信息显示在这里。
echo   关掉这个窗口，程序就退出了。
echo.
".venv\Scripts\python.exe" run.py
echo.
echo   程序已退出，上面是最后的输出。
pause
