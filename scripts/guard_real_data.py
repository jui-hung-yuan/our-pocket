"""Block real bank and broker exports from being committed.

Used as a pre-commit hook (paths passed as arguments) and by `make check` / CI with
`--all` (every file git would commit). A file is blocked when:

1. it has a statement-like extension (.csv .tsv .pdf .xls .xlsx) outside data/synthetic/;
2. it lives under data/ but outside data/synthetic/ (README.md excepted);
3. it *looks like* an export whatever its name: PDF magic bytes, or a first line whose
   header fingerprint matches a registered institution format;
4. it is under data/synthetic/ but is not byte-identical to its generator's output
   (our_pocket.synthetic.FIXTURES), so real data can't hide there either.

Exit code 1 lists every blocked file and why.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from our_pocket.etl.extract.csv_extractor import header_fingerprint, normalise_column
from our_pocket.etl.registry import default_registry
from our_pocket.synthetic import FIXTURES

ROOT = Path(__file__).resolve().parents[1]
STATEMENT_EXTENSIONS = {".csv", ".tsv", ".pdf", ".xls", ".xlsx"}
SYNTHETIC_DIR = "data/synthetic/"
ALLOWED_IN_DATA = {"data/README.md"}
DELIMITERS = (",", ";", "\t")
KNOWN_HEADERS = {t.fingerprint: t.schema_id for t in default_registry()}


def _looks_like_export(path: Path) -> str | None:
    with path.open("rb") as f:
        head = f.read(8192)
    if head.startswith(b"%PDF-"):
        return "PDF document"
    try:
        first_line = head.decode("utf-8-sig").splitlines()[0]
    except (UnicodeDecodeError, IndexError):
        return None
    for delimiter in DELIMITERS:
        columns = tuple(normalise_column(c.strip('"')) for c in first_line.split(delimiter))
        schema_id = KNOWN_HEADERS.get(header_fingerprint(columns, delimiter))
        if schema_id:
            return f"header matches the {schema_id} export format"
    return None


def check(rel: str, root: Path = ROOT) -> str | None:
    """Return why `rel` (a path relative to `root`) must not be committed, or None."""
    path = root / rel
    if not path.is_file():  # deleted or renamed away: nothing to commit
        return None
    in_synthetic = rel.startswith(SYNTHETIC_DIR)

    if in_synthetic:
        if Path(rel).suffix.lower() in STATEMENT_EXTENSIONS:
            build = FIXTURES.get(rel)
            if build is None:
                return (
                    "not a registered synthetic fixture (add it to our_pocket.synthetic.FIXTURES)"
                )
            if path.read_bytes() != build():
                return "differs from its generator's output; run `make synthetic`"
        return None

    if rel.startswith("data/") and rel not in ALLOWED_IN_DATA:
        return "files under data/ stay local (only data/synthetic/ is committed)"
    if Path(rel).suffix.lower() in STATEMENT_EXTENSIONS:
        return f"{Path(rel).suffix.lower()} files are only allowed under {SYNTHETIC_DIR}"
    reason = _looks_like_export(path)
    if reason:
        return f"looks like a real export ({reason})"
    return None


def _all_committable_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="repo-relative paths (pre-commit passes these)")
    parser.add_argument("--all", action="store_true", help="check every file git would commit")
    args = parser.parse_args(argv)

    paths = _all_committable_files() if args.all else args.paths
    blocked = [(p, reason) for p in paths if (reason := check(p))]
    for path, reason in blocked:
        print(f"BLOCKED {path}: {reason}", file=sys.stderr)
    if blocked:
        print(
            f"\n{len(blocked)} file(s) look like real financial data. Keep real exports in "
            "data/raw/ (git-ignored). See docs/checks.md.",
            file=sys.stderr,
        )
        return 1
    print(f"real-data guard: {len(paths)} file(s) checked, none blocked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
