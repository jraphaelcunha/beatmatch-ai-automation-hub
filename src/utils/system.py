"""
System-level environment and runtime utility helpers.
"""

import sys


def configure_utf8_stdout() -> None:
    """Configures stdout stream to UTF-8 encoding on platforms where default encoding differs."""
    if hasattr(sys.stdout, "encoding") and sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
