"""Extract → Transform → Load for institution exports.

    extract/    bytes → RawFile (raw string rows + file signature). Knows file *formats* (CSV).
    transform/  RawRow → ParsedRow + flags. Knows *institutions* and their schema versions.
    registry    picks the transformer whose schema matches a file's header fingerprint.
    load/       writes batches to the database (stage, then commit or reject).
    pipeline    ImportPipeline wires the three together.

extract/ and transform/ are pure (no database); only load/ touches it.
See docs/architecture.md.
"""
