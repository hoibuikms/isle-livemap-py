@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip || goto :fail
pip install -r requirements.txt pyinstaller || goto :fail
pyinstaller --noconfirm --clean IsleLiveMap.spec || goto :fail
echo.
echo Built: dist\IsleLiveMap\IsleLiveMap.exe
exit /b 0
:fail
echo Build failed.
exit /b 1
