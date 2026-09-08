"""Set or replace one KEY=value in a 0600 env file from a protected value file.

Never prints the value. Existing unrelated assignments are preserved. Usage:

    python3 scripts/update_protected_env.py --env-file PATH --key NAME --value-file PATH
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import tempfile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update one env assignment from a protected file")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--value-file", required=True)
    parser.add_argument("--create-from")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", args.key):
        print("refusing invalid env key", file=sys.stderr)
        return 3
    value = pathlib.Path(args.value_file).read_text(encoding="utf-8").strip()
    if not value or any(ch.isspace() for ch in value):
        print("value file is empty or contains whitespace", file=sys.stderr)
        return 3
    env_path = pathlib.Path(args.env_file)
    if not env_path.exists():
        if not args.create_from:
            print("env file missing and --create-from not given", file=sys.stderr)
            return 3
        pathlib.Path(args.create_from).read_text(encoding="utf-8")
        os.makedirs(env_path.parent, exist_ok=True)
        text = pathlib.Path(args.create_from).read_text(encoding="utf-8")
    else:
        text = env_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^#?\s*{0}=.*$".format(re.escape(args.key)), re.M)
    replacement = "{0}={1}".format(args.key, value)
    if pattern.search(text):
        text = pattern.sub(replacement, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += replacement + "\n"
    fd, tmp = tempfile.mkstemp(prefix=env_path.name + ".", dir=str(env_path.parent))
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
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
    print("{0} written to env file (value not printed)".format(args.key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
