"""Which transformer reads which file: matched by header fingerprint.

Supporting a changed export format means registering a new Transformer subclass with
a higher schema_version, never editing an existing one: old batches keep pointing at
the schema version they were read with.
"""

from collections.abc import Iterable, Iterator

from our_pocket.etl.extract import FileSignature
from our_pocket.etl.transform import N26V1, TradeRepublicV1, Transformer


class FormatRegistry:
    def __init__(self, transformers: Iterable[Transformer]) -> None:
        self._by_fingerprint: dict[str, Transformer] = {}
        for transformer in transformers:
            self.register(transformer)

    def register(self, transformer: Transformer) -> None:
        if transformer.fingerprint in self._by_fingerprint:
            existing = self._by_fingerprint[transformer.fingerprint]
            raise ValueError(
                f"{type(transformer).__name__} has the same header as {type(existing).__name__}"
            )
        self._by_fingerprint[transformer.fingerprint] = transformer

    def __iter__(self) -> Iterator[Transformer]:
        return iter(self._by_fingerprint.values())

    def match(self, signature: FileSignature) -> Transformer | None:
        return self._by_fingerprint.get(signature.header_fingerprint)

    def explain_mismatch(self, signature: FileSignature) -> str:
        """Describe how an unknown header differs from the closest registered schema."""
        columns = set(signature.columns)
        closest = max(self, key=lambda t: len(columns & set(t.columns)))
        missing = [c for c in closest.columns if c not in columns]
        extra = [c for c in signature.columns if c not in closest.columns]
        parts = [f"closest known schema: {closest.schema_id} v{closest.schema_version}"]
        if missing:
            parts.append(f"missing columns: {', '.join(missing)}")
        if extra:
            parts.append(f"extra columns: {', '.join(extra)}")
        if signature.delimiter != closest.delimiter:
            parts.append(f"delimiter {signature.delimiter!r} instead of {closest.delimiter!r}")
        if not missing and not extra and signature.delimiter == closest.delimiter:
            parts.append("same columns in a different order")
        return "; ".join(parts)


def default_registry() -> FormatRegistry:
    return FormatRegistry([TradeRepublicV1(), N26V1()])
