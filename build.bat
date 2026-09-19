@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   远程桌面控制 v4.0.2 打包脚本
echo ========================================
echo.

echo [1/3] 清理旧的构建产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist main.spec del /q main.spec
echo 完成.
echo.

echo [2/3] 正在打包 (PyInstaller -F -w)...
pyinstaller -F -w -i icon.ico --paths . ^
  --add-data "icon.ico;." ^
  --hidden-import logger --hidden-import config --hidden-import constants --hidden-import exceptions ^
  --collect-submodules core --collect-submodules gui --collect-submodules network ^
  --collect-submodules screen --collect-submodules service --collect-submodules input --collect-submodules utils ^
  --collect-submodules PIL --collect-submodules mss --collect-submodules pyautogui ^
  --collect-submodules pyperclip --collect-submodules bcrypt --collect-submodules cryptography ^
  main.py

if %errorlevel% neq 0 (
    echo.
    echo [错误] 打包失败！
    pause
    exit /b 1
)
echo.

echo [3/3] 打包完成！
echo 输出文件: %~dp0dist\main.exe
echo.
pause
