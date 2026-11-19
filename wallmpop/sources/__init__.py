"""Listing sources. Each source turns an AlertConfig into a list of Listing."""
from __future__ import annotations

from typing import Protocol

from ..models import AlertConfig, Listing


class SourceError(Exception):
    """The source did not answer as expected. `kind` is 'http', 'blocked', 'schema' or 'network'."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class Source(Protocol):
    name: str

    async def search(self, cfg: AlertConfig) -> list[Listing]: ...
