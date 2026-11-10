"""Tests de l'administration locale et de sa CLI sans secret en argument."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

from mevo2glpi_web.auth.admin import LocalAccountAdminService
from mevo2glpi_web.auth.cli import run_local_account_cli
from mevo2glpi_web.auth.local import LocalAccount

ACCOUNT_ID = UUID("7eb80eb4-6e43-49fe-a725-7709ddb43a84")
NOW = datetime(2026, 11, 10, 10, tzinfo=UTC)
PASSWORD = "une passphrase locale robuste"


class FakeHasher:
    def hash(self, password: str) -> str:
        return f"argon2:{password}"

    def verify(self, password: str, encoded_hash: str) -> bool:
        return encoded_hash == self.hash(password)


@dataclass
class FakeAdminRepository:
    accounts: dict[str, LocalAccount] = field(default_factory=dict)
    changes: list[dict[str, object]] = field(default_factory=list)

    def create(
        self,
        *,
        username: str,
        password_hash: str,
        occurred_at: datetime,
    ) -> LocalAccount:
        account = LocalAccount(
            account_id=ACCOUNT_ID,
            username=username,
            password_hash=password_hash,
            is_active=True,
            session_version=1,
        )
        self.accounts[username] = account
        self.changes.append({"operation": "create", "at": occurred_at})
        return account

    def list_all(self) -> list[LocalAccount]:
        return list(self.accounts.values())

    def get_by_username(self, username: str) -> LocalAccount | None:
        return self.accounts.get(username)

    def set_active(
        self,
        account_id: UUID,
        *,
        is_active: bool,
        increment_session_version: bool,
        occurred_at: datetime,
    ) -> LocalAccount:
        account = self._by_id(account_id)
        updated = replace(
            account,
            is_active=is_active,
            session_version=account.session_version + int(increment_session_version),
        )
        self.accounts[updated.username] = updated
        self.changes.append(
            {
                "operation": "set_active",
                "increment": increment_session_version,
                "at": occurred_at,
            }
        )
        return updated

    def replace_password(
        self,
        account_id: UUID,
        *,
        password_hash: str,
        increment_session_version: bool,
        occurred_at: datetime,
    ) -> LocalAccount:
        account = self._by_id(account_id)
        updated = replace(
            account,
            password_hash=password_hash,
            session_version=account.session_version + int(increment_session_version),
        )
        self.accounts[updated.username] = updated
        self.changes.append(
            {
                "operation": "replace_password",
                "increment": increment_session_version,
                "at": occurred_at,
            }
        )
        return updated

    def _by_id(self, account_id: UUID) -> LocalAccount:
        return next(
            account
            for account in self.accounts.values()
            if account.account_id == account_id
        )


def test_disable_and_password_reset_increment_session_version() -> None:
    repository = FakeAdminRepository()
    service = _service(repository)
    account = service.create(" Operator ", PASSWORD)

    disabled = service.disable(account.username)
    reset = service.reset_password(
        account.username,
        "une deuxieme passphrase robuste",
    )

    assert account.username == "operator"
    assert account.password_hash == f"argon2:{PASSWORD}"
    assert disabled.session_version == 2
    assert disabled.is_active is False
    assert reset.session_version == 3
    assert reset.password_hash == "argon2:une deuxieme passphrase robuste"
    assert all(change["at"] == NOW for change in repository.changes)


def test_cli_reads_password_with_getpass_and_never_prints_it() -> None:
    repository = FakeAdminRepository()
    outputs: list[str] = []
    prompts: list[str] = []

    def read_password(prompt: str) -> str:
        prompts.append(prompt)
        return PASSWORD

    result = run_local_account_cli(
        ["create", "operator"],
        _service(repository),
        password_reader=read_password,
        output=outputs.append,
    )

    assert result == 0
    assert prompts == ["Password: ", "Confirm password: "]
    assert PASSWORD not in "".join(outputs)
    assert outputs == ["Local account command completed: create operator"]


def test_cli_rejects_mismatched_confirmation_without_writing() -> None:
    repository = FakeAdminRepository()
    answers = iter([PASSWORD, "une autre valeur differente"])
    outputs: list[str] = []

    result = run_local_account_cli(
        ["create", "operator"],
        _service(repository),
        password_reader=lambda _prompt: next(answers),
        output=outputs.append,
    )

    assert result == 2
    assert repository.accounts == {}
    assert outputs == ["Password confirmation does not match."]


def _service(repository: FakeAdminRepository) -> LocalAccountAdminService:
    return LocalAccountAdminService(
        repository,
        FakeHasher(),
        clock=lambda: NOW,
    )
