import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

from click.testing import CliRunner
from lagom import Container

from ynab_cli.adapters.ynab import models
from ynab_cli.domain.settings import Settings, YnabSettings
from ynab_cli.domain.use_cases import accounts as use_cases
from ynab_cli.host.cli import cli


def test_list_all(runner: CliRunner, container: Container, empty_uuid: UUID) -> None:
    async def list_all(*args: Any, **kwargs: Any) -> AsyncIterator[models.Account]:
        yield models.Account(
            id=empty_uuid,
            name="Checking",
            type_=models.AccountType.CHECKING,
            on_budget=True,
            closed=False,
            balance=123_000,
            cleared_balance=100_000,
            uncleared_balance=23_000,
            transfer_payee_id=None,
            deleted=False,
        )

    use_case = MagicMock(wraps=list_all)
    container[use_cases.ListAll] = use_case

    result = runner.invoke(
        cli,
        ["run", "--access-token", "test_token", "accounts", "--budget-id", "test_budget", "list-all"],
    )

    assert result.exit_code == 0
    use_case.assert_called_once_with(
        Settings(ynab=YnabSettings(access_token="test_token", budget_id="test_budget")),
        {},
    )


def test_list_all_json_with_show_ids(runner: CliRunner, container: Container, empty_uuid: UUID) -> None:
    async def list_all(*args: Any, **kwargs: Any) -> AsyncIterator[models.Account]:
        yield models.Account(
            id=empty_uuid,
            name="Checking",
            type_=models.AccountType.CHECKING,
            on_budget=True,
            closed=False,
            balance=123_000,
            cleared_balance=100_000,
            uncleared_balance=23_000,
            transfer_payee_id=None,
            deleted=False,
        )

    use_case = MagicMock(wraps=list_all)
    container[use_cases.ListAll] = use_case

    result = runner.invoke(
        cli,
        [
            "run",
            "--access-token",
            "test_token",
            "--output",
            "json",
            "--show-ids",
            "accounts",
            "--budget-id",
            "test_budget",
            "list-all",
        ],
    )

    assert result.exit_code == 0
    use_case.assert_called_once_with(
        Settings(
            ynab=YnabSettings(access_token="test_token", budget_id="test_budget"),
            output_format="json",
            show_ids=True,
        ),
        {},
    )

    payload = json.loads(result.output)
    assert payload[0]["id"] == str(empty_uuid)
    assert payload[0]["name"] == "Checking"
