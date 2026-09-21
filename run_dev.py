"""Development server with auto-reload.

Use ``python run_dev.py``. The packaged app and ``python app.py`` do not reload.
"""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    os.environ.setdefault("JARVIS_DEBUG", "1")
    uvicorn.run(
        "app:app",
        host=os.environ.get("JARVIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("JARVIS_PORT", "8765")),
        reload=True,
        reload_dirs=["core", "api", "providers", "agents", "tools", "memory", "voice", "integrations"],
        log_config=None,
        access_log=False,
    )
