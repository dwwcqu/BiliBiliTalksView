"""Evidence supplied by trusted execution code, never decoded from model claims."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    attempt_id: str
    input_sha256: str
    authorized: bool = False
    delivery_verified: bool = False
    execution_ref: dict | None = None
    agent_id: str | None = None
    delivery_ref: dict | None = None
