"""Safe public error codes for output validation and publication."""

from app.analysis_packets.errors import PacketError


class OutputError(PacketError):
    """An output failed a structural, semantic or publication check."""
