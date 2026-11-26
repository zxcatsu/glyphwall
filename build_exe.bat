@echo off
chcp 65001 >nul
echo [1/3] Устанавливаю библиотеки...
py -m pip install -r requirements.txt pyinstaller || goto :err
echo [2/3] Собираю GlyphWall...
py -m PyInstaller --noconfirm --clean GlyphWall.spec || goto :err
echo [3/3] Готово: dist\GlyphWall\GlyphWall.exe
pause
exit /b 0
:err
echo Ошибка сборки. Проверь, что Python 3.10+ установлен с галкой "Add to PATH".
pause
