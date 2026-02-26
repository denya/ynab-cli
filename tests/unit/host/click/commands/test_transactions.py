import json
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from click.testing import CliRunner
from lagom import Container

from tests.factories import ynab
from ynab_cli.adapters.ynab import models
from ynab_cli.domain.models import rules
from ynab_cli.domain.settings import Settings, YnabSettings
from ynab_cli.domain.use_cases import transactions as use_cases
from ynab_cli.host.cli import cli


@pytest.fixture
def transaction_rules() -> rules.TransactionRules:
    return rules.TransactionRules(transaction_rules=[])


@pytest.fixture
def transaction_rules_file(runner: CliRunner, transaction_rules: rules.TransactionRules) -> Generator[Path]:
    with runner.isolated_filesystem() as tmp_path:
        with open("rules.json", "w") as rules_file:
            json.dump(transaction_rules.to_dict(), rules_file)
        yield Path(tmp_path) / "rules.json"


def test_apply_rules(
    runner: CliRunner, container: Container, transaction_rules: rules.TransactionRules, transaction_rules_file: Path
) -> None:
    async def apply_rules(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[tuple[models.TransactionDetail, models.SaveTransactionWithIdOrImportId]]:
        yield (
            ynab.TransactionDetailFactory.build(),
            models.SaveTransactionWithIdOrImportId(),
        )

    use_case = MagicMock(wraps=apply_rules)
    container[use_cases.ApplyRules] = use_case

    result = runner.invoke(
        cli,
        [
            "run",
            "--access-token",
            "test_token",
            "transactions",
            "--budget-id",
            "test_budget",
            "apply-rules",
            str(transaction_rules_file),
        ],
    )

    assert result.exit_code == 0

    use_case.assert_called_once_with(
        Settings(ynab=YnabSettings(access_token="test_token", budget_id="test_budget")),
        {"dry_run": False, "transaction_rules": transaction_rules},
    )


def test_delete_json_outputs_deleted_transaction(
    runner: CliRunner, container: Container, empty_uuid: UUID
) -> None:
    async def delete(*args: Any, **kwargs: Any) -> AsyncIterator[models.TransactionDetail]:
        yield cast(
            models.TransactionDetail,
            ynab.TransactionDetailFactory.build(
                id=empty_uuid,
                deleted=False,
                account_name="Checking",
                amount=-12345,
            ),
        )

    use_case = MagicMock(wraps=delete)
    container[use_cases.Delete] = use_case

    result = runner.invoke(
        cli,
        [
            "run",
            "--access-token",
            "test_token",
            "--output",
            "json",
            "transactions",
            "--budget-id",
            "test_budget",
            "delete",
            "--yes",
            str(empty_uuid),
        ],
    )

    assert result.exit_code == 0
    use_case.assert_called_once_with(
        Settings(
            ynab=YnabSettings(access_token="test_token", budget_id="test_budget"),
            output_format="json",
        ),
        {"transaction_id": str(empty_uuid)},
    )

    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["id"] == str(empty_uuid)
    assert payload[0]["account"] == "Checking"
    assert payload[0]["amount"] == -12.345
