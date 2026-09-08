"""The adapter registry.

The orchestrator names sources by their config code and never imports an adapter
directly, so adding a source is a config edit plus a class -- not a change to the
scheduler.

`build_enabled` is the only construction path, and it applies both guards:
config must mark the source enabled, and `FareSource.__init__` refuses any tier
above 2. Re-enabling an excluded source therefore requires defeating two
independent checks in two files, which is the point.
"""
from __future__ import annotations

from typing import Iterable

from apix.sources.akasa import AkasaAirSource
from apix.sources.base import BlockedByPolicy, FareSource
from apix.sources.spicejet import SpiceJetSource

#: code -> adapter class. Codes match config/sources.yaml and apix.source.code.
REGISTRY: dict[str, type[FareSource]] = {
    AkasaAirSource.code: AkasaAirSource,
    SpiceJetSource.code: SpiceJetSource,
}


def build_enabled(ctx, codes: Iterable[str] | None = None
                  ) -> dict[str, FareSource]:
    """Instantiate the adapters for the requested codes.

    A code with no adapter is skipped silently: config lists eleven sources and
    nine of them are excluded on compliance grounds and will never have one.
    A code whose adapter exists but whose tier forbids collection raises, loudly,
    because that combination means someone edited the tier.
    """
    wanted = set(codes) if codes is not None else set(REGISTRY)
    out: dict[str, FareSource] = {}
    for code in sorted(wanted):
        cls = REGISTRY.get(code)
        if cls is None:
            continue
        out[code] = cls(ctx)          # raises BlockedByPolicy above tier 2
    return out


__all__ = ["REGISTRY", "build_enabled", "BlockedByPolicy", "FareSource"]
