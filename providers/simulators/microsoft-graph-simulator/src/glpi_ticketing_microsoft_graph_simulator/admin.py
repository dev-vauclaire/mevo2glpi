"""Operator CLI for seeding and resetting the Microsoft Graph simulator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ADMIN_BASE_URL = "http://127.0.0.1:8080"


def main(argv: list[str] | None = None) -> int:
    """Execute one bounded administration request without printing secrets."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    token = os.environ.get("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", "")
    if not token:
        print("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN is required", file=sys.stderr)
        return 2
    try:
        if arguments.command == "seed":
            payload: dict[str, str] = {"extension": arguments.extension}
            if arguments.received_at is not None:
                payload["received_at"] = _received_at(arguments.received_at)
            result = _post_json(
                f"{DEFAULT_ADMIN_BASE_URL}/__admin/messages",
                token=token,
                payload=payload,
            )
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            return 0
        _post_json(
            f"{DEFAULT_ADMIN_BASE_URL}/__admin/reset",
            token=token,
            payload=None,
        )
    except (HTTPError, URLError, OSError, ValueError) as error:
        print(
            "Microsoft Graph simulator admin request failed "
            f"error_type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print("Microsoft Graph simulator state reset")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glpi-ticketing-microsoft-graph-simulator-admin",
        description="Seed or reset a synthetic staging mailbox.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    seed = subcommands.add_parser("seed", help="Create one synthetic Inbox message")
    seed.add_argument("--extension", choices=("wav", "ogg"), required=True)
    seed.add_argument(
        "--received-at",
        help="Optional timezone-aware ISO-8601 reception timestamp",
    )
    subcommands.add_parser("reset", help="Delete all synthetic messages")
    return parser


def _received_at(raw_value: str) -> str:
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("received-at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("received-at must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _post_json(
    url: str,
    *,
    token: str,
    payload: dict[str, str] | None,
) -> object:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": token,
        },
    )
    with urlopen(request, timeout=5) as response:
        response_body = response.read()
    if not response_body:
        return None
    return json.loads(response_body)


__all__ = ["DEFAULT_ADMIN_BASE_URL", "main"]
