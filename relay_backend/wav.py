"""本地 WAV 工具：生成短单声道演示音频，并校验上传内容。

只使用标准库，不做任何实时编解码。演示音频为单声道 16bit PCM、2 秒、
8kHz，每个口译员使用不同频率的正弦音，方便人耳/测试区分来源。
"""

from __future__ import annotations

import io
import math
import struct
import wave

SAMPLE_RATE = 8000
SLOT_SECONDS = 2
SAMPLE_WIDTH = 2  # 16bit PCM


class WavError(ValueError):
    """上传内容不是合法的单声道 PCM WAV。"""


def make_mono_wav(
    frequency: float,
    seconds: float = SLOT_SECONDS,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.4,
) -> bytes:
    """生成一段单声道 16bit PCM WAV（正弦音）。"""
    n_frames = int(round(seconds * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            # 轻微淡入淡出，避免爆音
            fade = min(1.0, i / 100.0, (n_frames - 1 - i) / 100.0)
            value = int(32767 * amplitude * fade * math.sin(2 * math.pi * frequency * i / sample_rate))
            frames += struct.pack("<h", value)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def validate_mono_wav(data: bytes) -> dict[str, int]:
    """校验 *data* 是单声道 PCM WAV 且非空，返回基本信息。"""
    if not data:
        raise WavError("empty body")
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            n_frames = wf.getnframes()
            sample_rate = wf.getframerate()
            # wave 模块对非 PCM 压缩格式读取会报出错误；这里再显式挡一次。
            if wf.getcomptype() != "NONE":
                raise WavError(f"compressed WAV not supported: {wf.getcomptype()}")
    except WavError:
        raise
    except Exception as exc:  # wave.Error 等
        raise WavError(f"not a valid WAV file: {exc}") from exc
    if n_channels != 1:
        raise WavError(f"mono WAV required, got {n_channels} channels")
    if sample_width not in (1, 2, 3, 4):
        raise WavError(f"unsupported sample width: {sample_width}")
    if n_frames <= 0:
        raise WavError("WAV contains no audio frames")
    return {
        "channels": n_channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "frames": n_frames,
        "duration_ms": round(n_frames * 1000 / sample_rate),
    }
