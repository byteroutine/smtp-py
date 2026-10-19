from ._version import __version__
from .client import configure, reset_configuration, send_bulk, send_email, send_email_async
from .config import get_settings

__all__ = [
    "__version__",
    "configure",
    "get_settings",
    "reset_configuration",
    "send_bulk",
    "send_email",
    "send_email_async",
]
