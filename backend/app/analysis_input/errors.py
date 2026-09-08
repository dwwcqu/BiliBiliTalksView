"""Safe machine-readable preparation errors."""


class InputPreparationError(ValueError):
    """Contains only a stable error code, never source text or credentials."""
