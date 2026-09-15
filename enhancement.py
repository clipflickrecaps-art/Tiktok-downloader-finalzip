"""Safe Pro enhancement pipeline.

The first production implementation uses FFmpeg CPU fallback. It never claims AI
quality restoration: original mode remuxes/copies, while upscale/enhance modes
apply bounded scaling and conservative filters only when requested.
"""
import asyncio
import os
import shutil
from pathlib import Path

MAX_INPUT_MB = int(os.environ.get("PRO_MAX_ENHANCE_INPUT_MB", "100"))
MAX_OUTPUT_MB = int(os.environ.get("PRO_MAX_ENHANCE_OUTPUT_MB", "200"))
TIMEOUT_SECONDS = int(os.environ.get("PRO_MAX_PROCESSING_SECONDS", "300"))
FFMPEG = shutil.which("ffmpeg")


def available() -> bool:
    return bool(FFMPEG)


def _check(path: str) -> None:
    size = Path(path).stat().st_size
    if size > MAX_INPUT_MB * 1024 * 1024:
        raise ValueError(f"Enhancement input exceeds {MAX_INPUT_MB} MB")


def _filters(mode: str) -> str | None:
    if mode == "original":
        return None
    if mode == "smart_upscale":
        return "scale=iw*2:ih*2:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
    if mode == "enhance":
        return "hqdn3d=1.2:1.2:3:3,unsharp=5:5:0.35:5:5:0"
    if mode == "upscale_enhance":
        return "scale=iw*2:ih*2:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,hqdn3d=1.0:1.0:2:2,unsharp=5:5:0.3"
    if mode == "fps60":
        return "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    raise ValueError("Unsupported enhancement mode")


async def enhance_video(input_path: str, output_path: str, mode: str = "original", preset: str = "balanced") -> str:
    """Return output path or raise a clear error; original mode avoids re-encode."""
    if not os.path.isfile(input_path):
        raise FileNotFoundError("Enhancement input is missing")
    _check(input_path)
    if mode == "original":
        shutil.copy2(input_path, output_path)
        return output_path
    if not available():
        raise RuntimeError("Enhancement unavailable. Original quality is still available.")
    vf = _filters(mode)
    crf = {"original": "18", "balanced": "22", "high": "19", "max": "17"}.get(preset, "22")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", input_path]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-c:a", "copy", output_path]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError("Enhancement processing timeout")
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace")[-500:] or "Enhancement failed")
    if not os.path.isfile(output_path):
        raise RuntimeError("Enhancement output was not created")
    if os.path.getsize(output_path) > MAX_OUTPUT_MB * 1024 * 1024:
        os.remove(output_path)
        raise ValueError(f"Enhancement output exceeds {MAX_OUTPUT_MB} MB")
    return output_path
