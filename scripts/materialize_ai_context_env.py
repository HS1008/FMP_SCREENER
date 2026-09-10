"""Copy only the AI Context API allowlist from a source env file into a 0600 dest file.

Never prints values. Provider keys, writer URLs, and IBKR ingest tokens are not copied.

    python3 scripts/materialize_ai_context_env.py --source PATH --dest PATH [--create-from PATH]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import tempfile

ALLOWLIST = (
    "DATABASE_READONLY_URL",
    "AI_CONTEXT_API_TOKEN",
    "AI_CONTEXT_API_HOST",
    "AI_CONTEXT_API_PORT",
    "AI_GATEWAY_PUBLIC_BASE_URL",
    "AI_GATEWAY_EXPORT_MODE",
    "AI_GATEWAY_RATE_LIMIT_PER_MINUTE",
    "AI_GATEWAY_CORS_ORIGINS",
    "AI_GATEWAY_OAUTH_ENABLED",
)
ASSIGNMENT = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$")


def _parse_assignments(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = ASSIGNMENT.match(line.strip())
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw.startswith("#"):
            continue
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]
        out[key] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize a least-privilege AI Context API env file")
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--create-from")
    args = parser.parse_args(argv)
    source = pathlib.Path(args.source)
    dest = pathlib.Path(args.dest)
    if not source.is_file():
        print("source env file missing", flush=True)
        return 3
    values = {k: v for k, v in _parse_assignments(source.read_text(encoding="utf-8")).items() if k in ALLOWLIST and v}
    if dest.exists():
        existing = _parse_assignments(dest.read_text(encoding="utf-8"))
        for key in ALLOWLIST:
            if existing.get(key) and key not in values:
                values[key] = existing[key]
    elif args.create_from:
        pathlib.Path(args.create_from).read_text(encoding="utf-8")
    lines = ["# Generated allowlist for fmp-ai-context-api.service. Do not add provider secrets.\n"]
    for key in ALLOWLIST:
        if values.get(key):
            lines.append("{0}={1}\n".format(key, values[key]))
    os.makedirs(dest.parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
    try:
        os.write(fd, "".join(lines).encode("utf-8"))
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    present = [key for key in ALLOWLIST if values.get(key)]
    print("ai context env materialized with keys: {0}".format(", ".join(present)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
