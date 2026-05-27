#!/bin/bash
# Install faster-whisper STT + Piper TTS (binary) on the Pi and download voice models.
# Run on the Pi: bash /opt/rover2/scripts/setup_voice.sh
set -euo pipefail

VENV="/opt/rover2/venv"
VOICES_DIR="/opt/rover2/voices"
WHISPER_MODELS_DIR="/opt/rover2/whisper-models"
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"

mkdir -p "$VOICES_DIR" "$WHISPER_MODELS_DIR"

echo "==> Installing ffmpeg (required by Whisper for audio decoding)..."
sudo apt install ffmpeg -y 2>/dev/null || echo "WARN: apt install ffmpeg failed — may already be installed"
if command -v ffmpeg &>/dev/null; then
  echo "    ffmpeg OK: $(ffmpeg -version 2>&1 | head -1)"
else
  echo "    WARN: ffmpeg not found after install attempt"
fi

echo ""
echo "==> Removing openai-whisper and torch (replacing with faster-whisper)..."
pip uninstall -y openai-whisper torch triton \
  nvidia-cublas-cu13 nvidia-cuda-cupti-cu13 nvidia-cuda-nvrtc-cu13 \
  nvidia-cuda-runtime-cu13 nvidia-cudnn-cu13 nvidia-cufft-cu13 \
  nvidia-curand-cu13 nvidia-cusolver-cu13 nvidia-cusparse-cu13 \
  nvidia-nccl-cu13 nvidia-nvjitlink-cu13 llvmlite numba \
  cuda-bindings cuda-pathfinder cuda-toolkit \
  2>/dev/null || true

echo ""
echo "==> Installing faster-whisper (ctranslate2-based, no torch)..."
pip install faster-whisper --break-system-packages --quiet || \
  echo "WARN: faster-whisper install failed"

echo ""
echo "==> Installing piper-tts (TTS — Python API, likely fails on cp313)..."
"$VENV/bin/pip" install piper-tts --quiet || echo "WARN: piper-tts install failed (piper binary used instead)"

echo ""
echo "==> Downloading English voice model (Piper TTS)..."
VOICE_FOUND=0

for voice in "en/en_GB/alan/medium/en_GB-alan-medium" "en/en_GB/cori/medium/en_GB-cori-medium"; do
  base="$(basename $voice)"
  if [ -f "$VOICES_DIR/${base}.onnx" ]; then
    echo "    ${base}.onnx already present"
    VOICE_FOUND=1
    break
  fi
  echo "    Trying $base ..."
  if wget -q --timeout=30 -O "$VOICES_DIR/${base}.onnx" "$HF_BASE/$voice.onnx" 2>/dev/null && \
     wget -q --timeout=30 -O "$VOICES_DIR/${base}.onnx.json" "$HF_BASE/$voice.onnx.json" 2>/dev/null; then
    echo "    Downloaded $base (GB English)"
    VOICE_FOUND=1
    break
  else
    rm -f "$VOICES_DIR/${base}.onnx" "$VOICES_DIR/${base}.onnx.json"
  fi
done

if [ "$VOICE_FOUND" -eq 0 ]; then
  echo "    GB voices unavailable — falling back to en_US-lessac-medium"
  base="en_US-lessac-medium"
  voice="en/en_US/lessac/medium/$base"
  if [ ! -f "$VOICES_DIR/${base}.onnx" ]; then
    wget -q --timeout=60 -O "$VOICES_DIR/${base}.onnx" "$HF_BASE/$voice.onnx"
    wget -q --timeout=60 -O "$VOICES_DIR/${base}.onnx.json" "$HF_BASE/$voice.onnx.json"
    echo "    Downloaded $base (US English fallback)"
  fi
fi

echo ""
echo "==> Voice models in $VOICES_DIR:"
ls -lh "$VOICES_DIR/" 2>/dev/null || echo "  (none)"

echo ""
echo "==> Pre-downloading faster-whisper tiny model to $WHISPER_MODELS_DIR..."
python3 -c "
from faster_whisper import WhisperModel
WhisperModel('tiny', device='cpu', compute_type='int8',
             download_root='/opt/rover2/whisper-models')
print('faster-whisper tiny ready.')
" && echo "    Model downloaded OK" || echo "    WARN: model pre-download failed (will download on first use)"

echo ""
echo "==> Verifying installation..."
python3 -c "
import shutil
try:
    from faster_whisper import WhisperModel
    print('faster-whisper import: OK')
except ImportError as e:
    print('faster-whisper import: FAILED —', e)
ff = shutil.which('ffmpeg')
print('ffmpeg:', ff if ff else 'NOT FOUND — transcription will fail')
"

echo ""
echo "==> Done. Restart rover2-api to activate:"
echo "    sudo systemctl restart rover2-api.service"
