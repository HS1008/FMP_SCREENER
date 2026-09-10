"""Set, replace, or delete KEY=value lines in a 0600 env file.

Never prints values. Existing unrelated assignments are preserved. Usage:

    python3 scripts/update_protected_env.py --env-file PATH --key NAME --value-file PATH
    python3 scripts/update_protected_env.py --env-file PATH --delete-key DATABASE_URL
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import tempfile


STREAMLIT_WRITER_KEYS = (
    "DATABASE_URL",
    "MARKET_INTELLIGENCE_DATABASE_URL",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_USER",
    "DB_NAME",
    "DB_PORT",
    "DASHBOARD_ALLOW_WRITER_FALLBACK",
    "STREAMLIT_ALLOW_PROVIDER_FETCH",
)


def _valid_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", key))


def upsert_env_assignment(text: str, key: str, value: str) -> str:
    if not _valid_key(key):
        raise ValueError("invalid env key")
    pattern = re.compile(r"^#?\s*{0}=.*$".format(re.escape(key)), re.M)
    replacement = "{0}={1}".format(key, value)
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + replacement + "\n"


def delete_env_keys(text: str, keys: list[str]) -> tuple[str, list[str]]:
    removed: list[str] = []
    for key in keys:
        if not _valid_key(key):
            raise ValueError("invalid env key")
        pattern = re.compile(r"^#?\s*{0}=.*(?:\n|$)".format(re.escape(key)), re.M)
        if pattern.search(text):
            text = pattern.sub("", text)
            removed.append(key)
    return text, removed


def _write_env(env_path: pathlib.Path, text: str) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update or scrub env assignments without printing values")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--key")
    parser.add_argument("--value-file")
    parser.add_argument("--create-from")
    parser.add_argument("--delete-key", action="append", default=[])
    parser.add_argument(
        "--scrub-streamlit-writer",
        action="store_true",
        default=False,
        help="Remove writer DB keys and STREAMLIT_ALLOW_PROVIDER_FETCH from a Streamlit-facing env file.",
    )
    args = parser.parse_args(argv)
    delete_keys = list(args.delete_key)
    if args.scrub_streamlit_writer:
        delete_keys.extend(STREAMLIT_WRITER_KEYS)
    if args.key:
        if not args.value_file:
            print("--value-file is required with --key", file=sys.stderr)
            return 3
        if not _valid_key(args.key):
            print("refusing invalid env key", file=sys.stderr)
            return 3
        value = pathlib.Path(args.value_file).read_text(encoding="utf-8").strip()
        if not value or any(ch.isspace() for ch in value):
            print("value file is empty or contains whitespace", file=sys.stderr)
            return 3
    elif not delete_keys:
        print("need --key/--value-file or --delete-key / --scrub-streamlit-writer", file=sys.stderr)
        return 3
    env_path = pathlib.Path(args.env_file)
    if not env_path.exists():
        if not args.create_from or not args.key:
            print("env file missing and --create-from not given", file=sys.stderr)
            return 3
        pathlib.Path(args.create_from).read_text(encoding="utf-8")
        os.makedirs(env_path.parent, exist_ok=True)
        text = pathlib.Path(args.create_from).read_text(encoding="utf-8")
    else:
        text = env_path.read_text(encoding="utf-8")
    if args.key:
        try:
            text = upsert_env_assignment(text, args.key, value)
        except ValueError:
            print("refusing invalid env key", file=sys.stderr)
            return 3
    if delete_keys:
        try:
            text, removed = delete_env_keys(text, delete_keys)
        except ValueError:
            print("refusing invalid env key", file=sys.stderr)
            return 3
    else:
        removed = []
    if args.scrub_streamlit_writer:
        text = upsert_env_assignment(text, "FMP_STREAMLIT_READONLY", "1")
    _write_env(env_path, text)
    if args.key:
        print("{0} written to env file (value not printed)".format(args.key))
    if removed:
        print("writer_keys_removed={0}".format(",".join(removed)))
    elif delete_keys:
        print("writer_keys_removed=")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
