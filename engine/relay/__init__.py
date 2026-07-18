"""Bruce self-hosted iMessage relay — Alpha (transport only; runs on a dedicated Mac).

Contains NO Bruce mission logic, model calls, or durable product state, and holds no cloud/DB/OpenAI
credentials — only a rotating device secret in the macOS Keychain. It watches Messages via the
audited `imsg` CLI, forwards normalized events to the Bruce API, and delivers outbound replies the
API queues. Live behaviour is UNVERIFIED until the dedicated-Mac test passes.
"""

from .backend import AuthError, Backend, BackendError, HttpBackend
from .checkpoint import FileCheckpoint
from .imsg import Imsg, ImsgEvent, SubprocessImsg, parse_event
from .relay import Relay

__all__ = [
    "AuthError", "Backend", "BackendError", "HttpBackend", "FileCheckpoint",
    "Imsg", "ImsgEvent", "SubprocessImsg", "parse_event", "Relay",
]
