"""Find the gateway checkout (pelegw/agent-authority-broker) for the tests
that need the broker itself: the manifest check with the broker's own loader
and the integration suite.

AAB_SRC names the checkout; by default it is a sibling directory,
../agent-authority-broker. Its `broker/` directory is put first on sys.path,
so the broker under test is always the one AAB_SRC names, even when another
copy is installed in the venv (the broker's dependencies must be installed:
`pip install -e "$AAB_SRC/broker"`). Nothing from the gateway is imported by
the plugin itself; this is test-only.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def gateway_root() -> Path | None:
    root = Path(os.environ.get("AAB_SRC") or REPO.parent / "agent-authority-broker").resolve()
    marker = root / "broker" / "broker" / "plugins" / "manifest.py"
    return root if marker.is_file() else None


def import_broker() -> tuple[object | None, str]:
    """(the broker package, "") or (None, why it is unavailable)."""
    root = gateway_root()
    if root is None:
        return None, "no gateway checkout (set AAB_SRC, default ../agent-authority-broker)"
    path = str(root / "broker")
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import broker
    except ImportError as exc:
        return None, f"gateway broker not importable ({exc}); pip install -e \"$AAB_SRC/broker\""
    if Path(broker.__file__).resolve().parents[1] != root / "broker":
        return None, f"a different broker package is already imported ({broker.__file__})"
    return broker, ""
