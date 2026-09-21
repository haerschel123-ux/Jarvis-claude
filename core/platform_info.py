"""Platform and hardware detection (Spec §94, §108).

Two jobs:

1. Tell the rest of JARVIS what this machine can actually do, so optional subsystems degrade
   honestly instead of pretending (``capabilities()``).
2. Recommend model sizes that fit the hardware, without downloading anything.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from functools import lru_cache
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def module_available(name: str) -> bool:
    """True if a module can be imported, without actually importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def has_display() -> bool:
    """True when a GUI session exists. GUI automation is impossible without one."""
    if IS_WINDOWS or IS_MACOS:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@lru_cache(maxsize=1)
def hardware() -> dict[str, Any]:
    """Best-effort hardware summary. Anything undetectable is reported as None, never guessed."""
    info: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "cpu_name": platform.processor() or None,
        "ram_total_gb": None,
        "disk_free_gb": None,
        "gpu": None,
        "vram_gb": None,
    }

    if module_available("psutil"):
        try:
            import psutil

            info["ram_total_gb"] = round(psutil.virtual_memory().total / 1024**3, 1)
        except Exception:
            pass
    elif IS_LINUX:
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        info["ram_total_gb"] = round(int(line.split()[1]) / 1024**2, 1)
                        break
        except OSError:
            pass

    try:
        info["disk_free_gb"] = round(shutil.disk_usage(os.getcwd()).free / 1024**3, 1)
    except OSError:
        pass

    gpu, vram = _detect_gpu()
    info["gpu"] = gpu
    info["vram_gb"] = vram
    return info


def _detect_gpu() -> tuple[str | None, float | None]:
    """Query nvidia-smi if present. Absence of a GPU is reported as None, not as an error."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None, None
    try:
        import subprocess

        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        line = out.stdout.strip().splitlines()[0]
        name, mem = (part.strip() for part in line.split(",", 1))
        return name, round(float(mem) / 1024, 1)
    except Exception:
        return None, None


def capabilities() -> dict[str, dict[str, Any]]:
    """What this installation can and cannot do, with an honest reason for each gap."""
    display = has_display()

    def cap(available: bool, reason: str = "", fix: str = "") -> dict[str, Any]:
        return {"available": available, "reason": reason if not available else "", "fix": fix if not available else ""}

    voice_stt = module_available("faster_whisper")
    voice_audio = module_available("sounddevice")
    return {
        "screen_capture": cap(
            (IS_WINDOWS or IS_MACOS or display) and module_available("PIL"),
            "Kein Grafik-Display oder Pillow fehlt",
            "pip install -r requirements-windows.txt",
        ),
        "mouse_keyboard": cap(
            display and module_available("pyautogui"),
            "GUI-Automation braucht eine Desktop-Sitzung" if not display else "pyautogui fehlt",
            "Auf dem Windows-Host starten und requirements-windows.txt installieren",
        ),
        "window_automation": cap(
            IS_WINDOWS and module_available("pywinauto"),
            "UI-Automation über Fenster-APIs gibt es nur unter Windows",
            "Auf dem Windows-Host starten",
        ),
        "process_control": cap(
            module_available("psutil"), "psutil fehlt", "pip install psutil"
        ),
        "speech_to_text": cap(
            voice_stt, "faster-whisper ist nicht installiert", "pip install -r requirements-voice.txt"
        ),
        "microphone": cap(
            voice_audio, "sounddevice ist nicht installiert", "pip install -r requirements-voice.txt"
        ),
        "wake_word": cap(
            module_available("openwakeword") or voice_stt,
            "Weder openWakeWord noch faster-whisper ist installiert",
            "pip install -r requirements-voice.txt",
        ),
        "text_to_speech": cap(
            module_available("piper") or IS_WINDOWS,
            "Kein TTS-Backend verfügbar",
            "pip install -r requirements-voice.txt",
        ),
        "desktop_shell": cap(
            module_available("webview"), "pywebview ist nicht installiert",
            "pip install -r requirements-windows.txt",
        ),
        "credential_store": cap(
            module_available("keyring"),
            "keyring fehlt; Secrets liegen in einer Datei mit 0600-Rechten",
            "pip install keyring",
        ),
    }


def recommendations() -> dict[str, str]:
    """Model-size suggestions that fit this machine. Nothing is downloaded automatically."""
    hw = hardware()
    ram = hw.get("ram_total_gb") or 0
    vram = hw.get("vram_gb") or 0

    if vram >= 10:
        whisper = "large-v3-turbo"
    elif vram >= 5 or ram >= 16:
        whisper = "medium"
    elif ram >= 8:
        whisper = "small"
    else:
        whisper = "base"

    if vram >= 20 or ram >= 48:
        ollama = "32B-Klasse (z. B. qwen2.5-coder:32b)"
    elif vram >= 10 or ram >= 32:
        ollama = "13–14B-Klasse"
    elif vram >= 6 or ram >= 16:
        ollama = "7–8B-Klasse"
    else:
        ollama = "3B-Klasse oder Cloud-Modelle bevorzugen"

    vision = "Lokale Visionmodelle sinnvoll" if (vram >= 8 or ram >= 32) else "Vision besser über die Cloud"
    return {
        "whisper_model": whisper,
        "ollama_class": ollama,
        "vision": vision,
        "stt_compute_type": "float16" if vram >= 6 else "int8",
    }


def summary() -> dict[str, Any]:
    return {
        "hardware": hardware(),
        "capabilities": capabilities(),
        "recommendations": recommendations(),
        "is_windows": IS_WINDOWS,
        "has_display": has_display(),
        "frozen": getattr(sys, "frozen", False),
    }
