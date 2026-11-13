@echo off
REM Build do FFmpeg Concat Pro para Windows (win64) com PyInstaller.
REM Requer: venv em build_env\venv com pyinstaller instalado, e bin\ffmpeg.exe + bin\ffprobe.exe
REM (build win64 estatico do BtbN: https://github.com/BtbN/FFmpeg-Builds/releases)

setlocal
cd /d "%~dp0"

set APP_NAME=FFmpegConcatPro
set VENV=%CD%\build_env\venv
set PY=%VENV%\Scripts\python.exe
set PYI=%VENV%\Scripts\pyinstaller.exe

if not exist "%PY%" (
    echo Venv nao encontrado em %VENV%.
    echo Crie com:
    echo   py -3.12 -m venv build_env\venv
    echo   %VENV%\Scripts\pip install --upgrade pip pyinstaller
    exit /b 1
)

if not exist "bin\ffmpeg.exe" (
    echo bin\ffmpeg.exe nao encontrado.
    exit /b 1
)
if not exist "bin\ffprobe.exe" (
    echo bin\ffprobe.exe nao encontrado.
    exit /b 1
)

echo == Verificando ffmpeg/ffprobe ==
bin\ffmpeg.exe -version
bin\ffprobe.exe -version

echo == Limpando builds anteriores ==
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q "%APP_NAME%.spec" 2>nul

echo == Rodando PyInstaller ==
"%PYI%" ^
    --name "%APP_NAME%" ^
    --windowed ^
    --onedir ^
    --noconfirm ^
    --clean ^
    --add-binary "bin\ffmpeg.exe;." ^
    --add-binary "bin\ffprobe.exe;." ^
    ffmpeg_concat_pro.py

echo.
echo == Build concluido ==
echo Executavel: %CD%\dist\%APP_NAME%\%APP_NAME%.exe

endlocal
