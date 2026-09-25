from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from click.testing import CliRunner
from lagom import Container

from ynab_cli.adapters.ynab import models
from ynab_cli.domain.settings import Settings, YnabSettings
from ynab_cli.domain.use_cases import categories as use_cases
from ynab_cli.host.cli import cli


def test_list_unused(runner: CliRunner, container: Container, empty_uuid: UUID) -> None:
    async def list_unused(*args: Any, **kwargs: Any) -> AsyncIterator[models.Category]:
        yield models.Category(
            id=empty_uuid,
            category_group_id=empty_uuid,
            name="Category",
            activity=0,
            balance=0,
            budgeted=0,
            hidden=False,
            deleted=False,
        )

    use_case = MagicMock(wraps=list_unused)
    container[use_cases.ListUnused] = use_case

    result = runner.invoke(
        cli, ["run", "--access-token", "test_token", "categories", "--budget-id", "test_budget", "list-unused"]
    )

    assert result.exit_code == 0
    use_case.assert_called_once_with(
        Settings(ynab=YnabSettings(access_token="test_token", budget_id="test_budget")), {}
    )


def test_list_all(runner: CliRunner, container: Container, empty_uuid: UUID) -> None:
    async def list_all(*args: Any, **kwargs: Any) -> AsyncIterator[models.Category]:
        yield models.Category(
            id=empty_uuid,
            category_group_id=empty_uuid,
            name="Category",
            activity=0,
            balance=0,
            budgeted=0,
            hidden=False,
            deleted=False,
        )

    use_case = MagicMock(wraps=list_all)
    container[use_cases.ListAll] = use_case

    result = runner.invoke(
        cli, ["run", "--access-token", "test_token", "categories", "--budget-id", "test_budget", "list-all"]
    )

    assert result.exit_code == 0
    use_case.assert_called_once_with(
        Settings(ynab=YnabSettings(access_token="test_token", budget_id="test_budget")), {}
    )


@pytest.mark.parametrize(("flags", "expected"), [([], False), (["--fuzzy-category"], True), (["--fuzzy"], True)])
def test_update_budget_fuzzy_category_flag(
    flags: list[str], expected: bool, runner: CliRunner, container: Container, empty_uuid: UUID
) -> None:
    async def update_budget(*args: Any, **kwargs: Any) -> AsyncIterator[models.Category]:
        yield models.Category(
            id=empty_uuid,
            category_group_id=empty_uuid,
            name="Groceries",
            hidden=False,
            budgeted=0,
            activity=0,
            balance=0,
            deleted=False,
        )

    use_case = MagicMock(wraps=update_budget)
    container[use_cases.UpdateBudget] = use_case

    result = runner.invoke(
        cli,
        [
            *["run", "--access-token", "t", "categories", "--budget-id", "b", "update-budget"],
            *["--category", "Grocery", "--amount", "10", *flags],
        ],
    )
    assert result.exit_code == 0, result.output
    assert use_case.call_args.args[1]["fuzzy_category"] is expected
