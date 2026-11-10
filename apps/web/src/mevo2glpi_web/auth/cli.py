"""Interface CLI injectable pour administrer les comptes locaux."""

from __future__ import annotations

import argparse
import getpass
import hmac
import os
import sys
from collections.abc import Callable, Sequence

from mevo2glpi_database import create_database_engine, create_session_factory

from .admin import LocalAccountAdminService
from .passwords import Argon2PasswordHasher
from .persistence import SqlAlchemyLocalAccountStore


def run_local_account_cli(
    argv: Sequence[str],
    service: LocalAccountAdminService,
    *,
    password_reader: Callable[[str], str] = getpass.getpass,
    output: Callable[[str], None] = print,
) -> int:
    """Execute une commande sans jamais accepter un mot de passe en argument."""

    arguments = _parser().parse_args(list(argv))
    if arguments.command == "list":
        for account in service.list_accounts():
            state = "enabled" if account.is_active else "disabled"
            output(f"{account.username}\t{state}")
        return 0

    username: str = arguments.username
    if arguments.command in {"create", "reset-password"}:
        password = password_reader("Password: ")
        confirmation = password_reader("Confirm password: ")
        if not hmac.compare_digest(
            password.encode("utf-8"),
            confirmation.encode("utf-8"),
        ):
            output("Password confirmation does not match.")
            return 2
        if arguments.command == "create":
            service.create(username, password)
        else:
            service.reset_password(username, password)
    elif arguments.command == "enable":
        service.enable(username)
    elif arguments.command == "disable":
        service.disable(username)
    else:  # pragma: no cover - argparse garantit les commandes connues.
        raise AssertionError("unknown local account command")

    output(f"Local account command completed: {arguments.command} {username}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mevo2glpi-web-users")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list local accounts")
    for name in ("create", "enable", "disable", "reset-password"):
        command = commands.add_parser(name)
        command.add_argument("username")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Compose the local-user CLI without loading unrelated web settings."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Local account configuration is invalid.", file=sys.stderr)
        return 2

    engine = None
    try:
        engine = create_database_engine(database_url, connect_timeout_seconds=5)
        session_factory = create_session_factory(engine)
        service = LocalAccountAdminService(
            SqlAlchemyLocalAccountStore(session_factory),
            Argon2PasswordHasher(),
        )
        return run_local_account_cli(
            tuple(argv) if argv is not None else tuple(sys.argv[1:]),
            service,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            f"Local account command failed ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


__all__ = ["main", "run_local_account_cli"]
