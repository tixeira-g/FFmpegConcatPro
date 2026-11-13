#!/bin/bash
# Build do FFmpeg Concat Pro para macOS (arm64) com PyInstaller.
# Requer: venv em build_env/venv com pyinstaller instalado, e bin/ffmpeg + bin/ffprobe.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="FFmpegConcatPro"
VENV="$SCRIPT_DIR/build_env/venv"
PY="$VENV/bin/python"
PYI="$VENV/bin/pyinstaller"

if [ ! -x "$PY" ]; then
    echo "Venv não encontrado em $VENV."
    echo "Crie com:"
    echo "  /opt/homebrew/bin/python3.12 -m venv build_env/venv"
    echo "  $VENV/bin/pip install --upgrade pip pyinstaller"
    exit 1
fi

if [ ! -x "bin/ffmpeg" ] || [ ! -x "bin/ffprobe" ]; then
    echo "bin/ffmpeg e/ou bin/ffprobe não encontrados ou não executáveis."
    echo "Baixe os binários estáticos arm64 e coloque em bin/ antes de buildar."
    exit 1
fi

echo "== Verificando ffmpeg/ffprobe =="
./bin/ffmpeg -version | head -1
./bin/ffprobe -version | head -1

echo "== Limpando builds anteriores =="
rm -rf build dist "${APP_NAME}.spec"

echo "== Rodando PyInstaller =="
"$PYI" \
    --name "$APP_NAME" \
    --windowed \
    --onedir \
    --noconfirm \
    --clean \
    --add-binary "bin/ffmpeg:." \
    --add-binary "bin/ffprobe:." \
    ffmpeg_concat_pro.py

APP_PATH="dist/${APP_NAME}.app"
echo
echo "== Build concluído =="
echo "App: $SCRIPT_DIR/$APP_PATH"
du -sh "$APP_PATH"
