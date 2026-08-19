#!/usr/bin/env python3
"""Run the signal engine:  python3 main.py

If dependencies were installed into .venv by setup.sh, this re-launches itself
with that interpreter, so plain `python3 main.py` works without activating it.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _use_venv_if_needed() -> None:
    if os.environ.get("OFS_RELAUNCHED"):
        return
    try:
        import ccxt  # noqa: F401
        return
    except ImportError:
        pass
    if VENV_PY.exists():
        os.environ["OFS_RELAUNCHED"] = "1"
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.exit("Dependencies missing. Run ./setup.sh first.")


_use_venv_if_needed()
sys.path.insert(0, str(ROOT / "src"))

from ofsignals.main import main  # noqa: E402

if __name__ == "__main__":
    main()
