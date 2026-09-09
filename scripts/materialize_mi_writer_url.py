"""Resolve the Market Intelligence writer URL and write it to a 0600 file.

Never prints the URL. Used by host provision so ``fmp-mi-refresh.service``
(which loads only ``/etc/fmp/market_intelligence.env``) has a writer identity
even when the dashboard ``.env`` uses ``DB_*`` instead of ``DATABASE_URL``.

Resolution order:
    1. Non-placeholder ``MARKET_INTELLIGENCE_DATABASE_URL``
    2. Non-placeholder ``DATABASE_URL``
    3. ``DB_HOST`` + ``DB_NAME`` + ``DB_USER`` (+ optional ``DB_PASSWORD`` / ``DB_PORT``)
"""

from __future__ import annotations

import argparse
import os
import pathlib
import urllib.parse


def _usable(value: str | None) -> str:
    text = (value or "").strip()
    if not text or "CHANGE_ME" in text:
        return ""
    return text


def resolve_writer_url(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if env is None else env
    dedicated = _usable(env.get("MARKET_INTELLIGENCE_DATABASE_URL"))
    if dedicated:
        return dedicated, "dedicated"
    database_url = _usable(env.get("DATABASE_URL"))
    if database_url:
        return database_url, "database_url"
    host = (env.get("DB_HOST") or "").strip()
    name = (env.get("DB_NAME") or "").strip()
    user = (env.get("DB_USER") or "").strip()
    if host and name and user:
        password = env.get("DB_PASSWORD") or ""
        port = (env.get("DB_PORT") or "5432").strip() or "5432"
        url = "postgresql://{0}:{1}@{2}:{3}/{4}".format(
            urllib.parse.quote(user, safe=""),
            urllib.parse.quote(password, safe=""),
            host,
            port,
            name,
        )
        return url, "db_star"
    return "", "missing"


def write_writer_url(output: pathlib.Path, env: dict[str, str] | None = None) -> str:
    url, source = resolve_writer_url(env)
    if not url:
        raise SystemExit(3)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(url + "\n", encoding="utf-8")
    os.chmod(output, 0o600)
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the MI writer URL to a protected file")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        source = write_writer_url(pathlib.Path(args.output))
    except SystemExit as exc:
        if exc.code == 3:
            print("writer_url_source=missing")
            return 3
        raise
    print("writer_url_source={0}".format(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
