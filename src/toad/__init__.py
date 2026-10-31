from typing import Literal, Mapping
import platform

NAME = "toad"
TITLE = "Hefesto"
ICON = "⚙"
"""Symbol shown wherever Toad used its frog."""
WAKE_NAME = "Vulcano"
"""What you call the assistant by voice ("Íris, modo planejamento").

The wake word watcher (Vosk) needs it in its vocabulary: `iris-voz` warns otherwise.
"""
WAKE_SPELLINGS = ("vulcano", "volcano", "vulcao", "vulcan", "bulcano", "vucano", "vulgano")
"""How Whisper may write the name (normalized: lowercase, no accents)."""

type OS = Literal["linux", "macos", "windows", "*"]

_system = platform.system()
_OS_map: dict[str, OS] = {
    "Linux": "linux",
    "Darwin": "macos",
    "Windows": "windows",
}
os: OS = _OS_map.get(_system, "linux")


def get_os_matrix(matrix: Mapping[OS, str]) -> str | None:
    """Get a value from a mapping where the key is an OS, falling back to a wildcard ("*").

    Args:
        matrix: A mapping where an OS literal is the key.

    Returns:
        The value, if one is found, or `None`.
    """
    if (result := matrix.get(os)) is None:
        result = matrix.get("*")
    return result


def get_version() -> str:
    """Get the current version of Toad.

    Returns:
        str: Version string, e.g "1.2.3"
    """
    from importlib.metadata import version

    try:
        return version("hefesto-cli")
    except Exception:
        try:
            return version("toad")
        except Exception:
            return "0.1.0unknown"
