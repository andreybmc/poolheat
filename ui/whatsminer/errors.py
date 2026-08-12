from __future__ import annotations

class WhatsminerError(Exception):
    """Base error for the library."""


class AuthError(WhatsminerError):
    """Token / password / permission failure."""


class ProtocolError(WhatsminerError):
    """Malformed or unexpected transport/protocol response."""


class CommandError(WhatsminerError):
    """Miner rejected a command (STATUS=E or non-zero code)."""

    def __init__(self, message: str, code: int | None = None, raw: dict | None = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}
