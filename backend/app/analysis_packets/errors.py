"""Public exceptions contain only safe machine-readable codes."""

import re


class PacketError(ValueError):
    """Do not include source text, host paths, or exception details in public errors."""

    def __init__(self, code: str):
        safe = (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]*", code)
            else "packet_error"
        )
        self.code = safe
        super().__init__(safe)
