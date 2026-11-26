@echo off
chcp 65001 >nul
setlocal

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Не найден Inno Setup 6. Поставь его:
    echo    winget install JRSoftware.InnoSetup
    pause
    exit /b 1
)

if not exist "dist\GlyphWall\GlyphWall.exe" (
    echo Сначала собери программу: build_exe.bat
    pause
    exit /b 1
)

echo Собираю установщик...
"%ISCC%" "installer\GlyphWall.iss" || goto :err
echo.
echo Готово: installer\GlyphWall-Setup.exe
pause
exit /b 0

:err
echo Ошибка сборки установщика.
pause
