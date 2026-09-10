"""Always-on SHA-256 pins for committed contract JSON.

FMP-only CI cannot checkout quant-strategies. These digests fail closed if a
contract file changes without updating contract_digests.json in the same change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


CONTRACTS = Path(__file__).resolve().parent
DIGESTS = CONTRACTS / "contract_digests.json"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_contract_digests() -> dict[str, Any]:
    return json.loads(DIGESTS.read_text(encoding="utf-8"))


def verify_contract_digests() -> dict[str, str]:
    pinned = load_contract_digests()
    expected = dict(pinned.get("files") or {})
    if not expected:
        raise ValueError("contract_digests.json has no files")
    checked: dict[str, str] = {}
    for name, digest in expected.items():
        path = CONTRACTS / name
        actual = file_sha256(path)
        if actual != digest:
            raise ValueError(
                "Contract digest mismatch for {0}: expected {1}, got {2}. "
                "Update contract_digests.json in the same change.".format(name, digest, actual)
            )
        checked[name] = actual
    from qc_research.contracts.sealed_results import verify_committed_tree_digests

    trees = verify_committed_tree_digests()
    checked["committed_trees"] = ",".join(sorted(trees))
    return checked


def main(argv: list[str] | None = None) -> int:
    del argv
    checked = verify_contract_digests()
    print("contract_digests=ok count={0}".format(len(checked)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
