"""Offline capacity proxy, never an execution-time tokenizer guarantee."""

from dataclasses import dataclass

METHOD = "utf8-byte-estimate-v1"


@dataclass(frozen=True)
class Budget:
    max_input_tokens: int
    reserved_output_tokens: int
    context_window: int
    max_active_members: int

    def __post_init__(self):
        if (
            any(
                type(v) is not int or v <= 0
                for v in (
                    self.max_input_tokens,
                    self.reserved_output_tokens,
                    self.context_window,
                    self.max_active_members,
                )
            )
            or self.max_input_tokens + self.reserved_output_tokens > self.context_window
        ):
            raise ValueError("invalid_budget")

    def limits(self) -> dict:
        return {
            "max_active_members": self.max_active_members,
            "max_input_tokens": self.max_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "accounting_method": METHOD,
        }


def estimate_input(serialized_packet: bytes, loaded_rules: bytes) -> int:
    return len(serialized_packet) + len(loaded_rules)
