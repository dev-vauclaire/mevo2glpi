"""Operator CLI for seeding and resetting the Microsoft Graph simulator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .audio import SyntheticAudioExtension
from .scenarios import MAX_SEED_AUDIO_BYTES, SeedScenario

DEFAULT_ADMIN_BASE_URL = "http://127.0.0.1:8080"


def main(argv: list[str] | None = None) -> int:
    """Execute one bounded administration request without printing secrets."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "list-scenarios":
        _print_scenarios()
        return 0

    token = os.environ.get("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", "")
    if not token:
        print("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN is required", file=sys.stderr)
        return 2
    try:
        if arguments.command == "seed":
            received_at = (
                _received_at(arguments.received_at)
                if arguments.received_at is not None
                else None
            )
            if arguments.scenario is not None:
                payload: dict[str, str] = {"scenario": arguments.scenario}
                if received_at is not None:
                    payload["received_at"] = received_at
                result = _post_json(
                    f"{DEFAULT_ADMIN_BASE_URL}/__admin/messages",
                    token=token,
                    payload=payload,
                )
            else:
                extension, content = _read_audio_path(arguments.audio_path)
                result = _post_audio(
                    f"{DEFAULT_ADMIN_BASE_URL}/__admin/messages/audio",
                    token=token,
                    extension=extension,
                    content=content,
                    received_at=received_at,
                )
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            return 0
        if arguments.command == "seed-suite":
            _seed_suite(token)
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
        prog="mevo2glpi-microsoft-graph-simulator-admin",
        description="Seed or reset a synthetic staging mailbox.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    seed = subcommands.add_parser("seed", help="Create one synthetic Inbox message")
    source = seed.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--scenario",
        choices=tuple(scenario.value for scenario in SeedScenario),
    )
    source.add_argument(
        "--audio-path",
        help="Path to a non-sensitive WAV or OGG test recording",
    )
    seed.add_argument(
        "--received-at",
        help="Optional timezone-aware ISO-8601 reception timestamp",
    )
    subcommands.add_parser(
        "seed-suite",
        help="Create one synthetic Inbox message for every deterministic scenario",
    )
    subcommands.add_parser(
        "list-scenarios",
        help="List deterministic message scenarios",
    )
    subcommands.add_parser("reset", help="Delete all synthetic messages")
    return parser


def _print_scenarios() -> None:
    payload = [
        {"description": scenario.description, "name": scenario.value}
        for scenario in SeedScenario
    ]
    print(json.dumps(payload, indent=2, sort_keys=True))


def _seed_suite(token: str) -> None:
    """Seed every scenario in catalogue order and print a sanitized manifest."""
    scenarios = tuple(SeedScenario)
    first_received_at = datetime.now(UTC) - timedelta(seconds=len(scenarios))
    messages: list[dict[str, object]] = []
    complete = False
    try:
        for index, scenario in enumerate(scenarios):
            received_at = first_received_at + timedelta(seconds=index)
            result = _post_json(
                f"{DEFAULT_ADMIN_BASE_URL}/__admin/messages",
                token=token,
                payload={
                    "scenario": scenario.value,
                    "received_at": _format_utc(received_at),
                },
            )
            messages.append(_suite_message(result, expected_scenario=scenario))
        complete = True
    finally:
        print(
            json.dumps(
                {
                    "complete": complete,
                    "messages": messages,
                    "scenario_count": len(scenarios),
                    "seeded_count": len(messages),
                },
                indent=2,
                sort_keys=True,
            )
        )


def _suite_message(
    result: object,
    *,
    expected_scenario: SeedScenario,
) -> dict[str, object]:
    """Select the bounded public fields expected from one seed response."""
    if not isinstance(result, dict):
        raise ValueError("seed-suite response must be an object")
    message_id = result.get("message_id")
    scenario = result.get("scenario")
    attachment_names = result.get("attachment_names")
    attachment_count = result.get("attachment_count")
    received_at = result.get("received_at")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("seed-suite response has an invalid message_id")
    if scenario != expected_scenario.value:
        raise ValueError("seed-suite response has an unexpected scenario")
    if not isinstance(attachment_names, list) or not all(
        isinstance(name, str) for name in attachment_names
    ):
        raise ValueError("seed-suite response has invalid attachment names")
    if (
        isinstance(attachment_count, bool)
        or not isinstance(attachment_count, int)
        or attachment_count < 0
        or attachment_count != len(attachment_names)
    ):
        raise ValueError("seed-suite response has an invalid attachment count")
    if not isinstance(received_at, str) or not received_at:
        raise ValueError("seed-suite response has an invalid reception timestamp")
    return {
        "attachment_count": attachment_count,
        "attachment_names": attachment_names,
        "message_id": message_id,
        "received_at": received_at,
        "scenario": scenario,
    }


def _format_utc(value: datetime) -> str:
    """Return one explicit UTC timestamp accepted by the admin endpoint."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_audio_path(raw_path: str) -> tuple[SyntheticAudioExtension, bytes]:
    path = Path(raw_path)
    if not path.is_file():
        raise ValueError("audio-path must reference a regular file")
    try:
        extension = SyntheticAudioExtension(path.suffix.removeprefix(".").casefold())
    except ValueError:
        raise ValueError("audio-path must use the wav or ogg extension") from None
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValueError("audio-path must not be empty")
    if size_bytes > MAX_SEED_AUDIO_BYTES:
        raise ValueError("audio-path exceeds the maximum size")
    content = path.read_bytes()
    if not content or len(content) > MAX_SEED_AUDIO_BYTES:
        raise ValueError("audio-path changed while it was being read")
    return extension, content


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
    return _send(request)


def _post_audio(
    url: str,
    *,
    token: str,
    extension: SyntheticAudioExtension,
    content: bytes,
    received_at: str | None,
) -> object:
    headers = {
        "Content-Type": extension.media_type,
        "X-Admin-Token": token,
        "X-Audio-Extension": extension.value,
    }
    if received_at is not None:
        headers["X-Received-At"] = received_at
    request = Request(
        url,
        data=content,
        method="POST",
        headers=headers,
    )
    return _send(request)


def _send(request: Request) -> object:
    with urlopen(request, timeout=30) as response:
        response_body = response.read()
    if not response_body:
        return None
    return json.loads(response_body)


__all__ = ["DEFAULT_ADMIN_BASE_URL", "main"]
