"""In-memory offline assembly and explicit trusted result boundaries."""

from dataclasses import dataclass, field


@dataclass
class Assembly:
    run_id: str
    packets: list[dict] = field(default_factory=list)
    task_rows: list[dict] = field(default_factory=list)
    target_indexes: dict[str, list[dict]] = field(default_factory=dict)
    group_coverage: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    resource_files: dict[str, bytes] = field(default_factory=dict)
    member_registry: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    context_window: int = 0
    previous_manifest_sha256: str | None = None
    output_schemas: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptedResult:
    packet: dict
    result_path: str
    result_bytes: bytes
    observations: tuple[dict, ...]
    status: str = "accepted"


AcceptedCatalog = dict[str, AcceptedResult]
