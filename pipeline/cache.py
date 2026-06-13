"""Disk cache for external calls, so reruns are free and deterministic."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable


class Cache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        directory = self.root / namespace
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{digest}.json"

    def get(self, namespace: str, key: str):
        path = self._path(namespace, key)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def set(self, namespace: str, key: str, value) -> None:
        self._path(namespace, key).write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    def get_or_compute(self, namespace: str, key: str, producer: Callable[[], object]):
        cached = self.get(namespace, key)
        if cached is not None:
            return cached
        value = producer()
        self.set(namespace, key, value)
        return value
