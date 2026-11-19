"""Tracks whether a source is answering as expected, to warn the user (developer notice)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

FAIL_THRESHOLD = 3          # consecutive failures before warning
EMPTY_THRESHOLD = 12        # consecutive runs with 0 items across every alert


@dataclass
class SourceHealth:
    name: str
    failures: int = 0
    empty_runs: int = 0
    warned: bool = False
    last_error: str = ""
    since: float = field(default_factory=time.time)

    def ok(self, n_items: int) -> str | None:
        """Returns a recovery message when it comes back after a warning."""
        self.failures = 0
        self.empty_runs = self.empty_runs + 1 if n_items == 0 else 0
        if self.empty_runs >= EMPTY_THRESHOLD and not self.warned:
            self.warned = True
            self.last_error = "empty"
            return self._warn("devuelve 0 anuncios en todas las búsquedas desde hace rato. "
                              "Puede que haya cambiado el formato o que nos esté filtrando.")
        if self.warned and n_items > 0:
            self.warned = False
            return f"✅ Aviso de desarrollador: la API de {self.name} vuelve a responder bien."
        return None

    def fail(self, kind: str, detail: str) -> str | None:
        self.failures += 1
        self.last_error = f"{kind}: {detail}"
        # A schema change won't fix itself by retrying: warn at once.
        if not self.warned and (kind == "schema" or self.failures >= FAIL_THRESHOLD):
            self.warned = True
            what = {
                "schema": "responde con un formato distinto al esperado",
                "blocked": "nos está bloqueando o limitando",
                "network": "no es alcanzable",
                "http": "devuelve errores",
            }.get(kind, "falla")
            return self._warn(f"{what}.\nDetalle: {detail[:300]}")
        return None

    def _warn(self, text: str) -> str:
        return (f"⚠️ Aviso de desarrollador: la API de {self.name} {text}\n"
                "Las alertas siguen activas y se reintenta sola; si persiste hay que tocar el código.")

    def status(self) -> str:
        if self.warned:
            return f"⚠️ {self.name}: con problemas ({self.last_error[:120]})"
        return f"✅ {self.name}: OK"
