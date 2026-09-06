"""Ephemeral proof bound to one client, task revision, and failure."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .source import CollectionStopped

_AUTHORITY = object()


def _credential_tag(client):
    jar = sorted((cookie.domain, cookie.path, cookie.name, cookie.value, cookie.secure,
                  cookie.expires, cookie.port, cookie.domain_specified, cookie.path_specified)
                 for cookie in client.cookies.jar)
    value = {"header": client.headers.get("cookie", ""), "jar": jar}
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).digest()


class _RecoveryProof:
    def __init__(self, authority, task, client, progress, target):
        if authority is not _AUTHORITY:
            raise CollectionStopped("invalid_recovery_proof")
        self.task = Path(task).resolve()
        self.client = client
        self.credential_tag = _credential_tag(client)
        self.failure_id = progress["failure"]["failure_id"]
        self.revision = progress.get("checkpoint_revision", 0)
        self.target = deepcopy(target)
        self.used = False

    def __repr__(self):
        return "<ephemeral recovery proof>"


def mint_proof(task, client, progress, target):
    return _RecoveryProof(_AUTHORITY, task, client, progress, target)


def authorize(proof, task, client, progress):
    failure = progress.get("failure") or {}
    target = {"phase": failure.get("phase"), **failure.get("target", {})}
    valid = (
        type(proof) is _RecoveryProof
        and not proof.used
        and proof.task == Path(task).resolve()
        and proof.client is client
        and proof.credential_tag == _credential_tag(client)
        and proof.failure_id == failure.get("failure_id")
        and proof.revision == progress.get("checkpoint_revision", 0)
        and proof.target == target
    )
    if not valid:
        raise CollectionStopped("invalid_recovery_proof")
    proof.used = True
