"""StormDesk entry point — python main.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stormdesk.config import HOST, PORT  # noqa: E402


def main() -> None:
    import uvicorn

    print(f"\n  StormDesk  →  http://{HOST}:{PORT}\n")
    uvicorn.run(
        "stormdesk.app:app",
        host=HOST,
        port=PORT,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
