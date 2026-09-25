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


def test_delete_json_outputs_deleted_transaction(runner: CliRunner, container: Container, empty_uuid: UUID) -> None:
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


def _base_args(*extra: str, json_output: bool = False) -> list[str]:
    args = ["run", "--access-token", "test_token"]
    if json_output:
        args += ["--output", "json"]
    return [*args, "transactions", "--budget-id", "test_budget", *extra]


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], {"fuzzy_payee": False, "fuzzy_category": False, "fuzzy_account": False}),
        (["--fuzzy-payee"], {"fuzzy_payee": True, "fuzzy_category": False, "fuzzy_account": False}),
        (
            ["--fuzzy-category", "--fuzzy-account"],
            {"fuzzy_payee": False, "fuzzy_category": True, "fuzzy_account": True},
        ),
        (["--fuzzy"], {"fuzzy_payee": True, "fuzzy_category": True, "fuzzy_account": True}),
    ],
)
def test_create_passes_fuzzy_flags(
    flags: list[str], expected: dict[str, bool], runner: CliRunner, container: Container
) -> None:
    async def create(*args: Any, **kwargs: Any) -> AsyncIterator[models.TransactionDetail]:
        yield cast(models.TransactionDetail, ynab.TransactionDetailFactory.build(deleted=False))

    use_case = MagicMock(wraps=create)
    container[use_cases.Create] = use_case

    result = runner.invoke(
        cli,
        _base_args("create", "--account", "Checking", "--payee", "Terra Natura", "--amount", "-5", *flags),
    )

    assert result.exit_code == 0, result.output
    params = use_case.call_args.args[1]
    assert params["payee_name"] == "Terra Natura"
    assert {k: params[k] for k in expected} == expected


def test_update_passes_fuzzy_flags(runner: CliRunner, container: Container) -> None:
    async def update(*args: Any, **kwargs: Any) -> AsyncIterator[models.TransactionDetail]:
        yield cast(models.TransactionDetail, ynab.TransactionDetailFactory.build(deleted=False))

    use_case = MagicMock(wraps=update)
    container[use_cases.Update] = use_case

    result = runner.invoke(cli, _base_args("update", "txn-1", "--payee", "Topmobil", "--fuzzy-payee"))

    assert result.exit_code == 0, result.output
    params = use_case.call_args.args[1]
    assert params["payee_name"] == "Topmobil"
    assert params["fuzzy_payee"] is True
    assert params["fuzzy_category"] is False


@pytest.mark.parametrize(("flags", "expected"), [([], False), (["--fuzzy-account"], True), (["--fuzzy"], True)])
def test_transfer_passes_fuzzy_account(
    flags: list[str], expected: bool, runner: CliRunner, container: Container
) -> None:
    async def transfer(*args: Any, **kwargs: Any) -> AsyncIterator[models.TransactionDetail]:
        yield cast(models.TransactionDetail, ynab.TransactionDetailFactory.build(deleted=False))

    use_case = MagicMock(wraps=transfer)
    container[use_cases.Transfer] = use_case

    result = runner.invoke(cli, _base_args("transfer", "--from", "A", "--to", "B", "--amount", "5", *flags))
    assert result.exit_code == 0, result.output
    assert use_case.call_args.args[1]["fuzzy_account"] is expected


def _bulk_result(payee_name: str, status: str, requested: str) -> use_cases.BulkCreateResult:
    return use_cases.BulkCreateResult(
        transaction=cast(
            models.TransactionDetail,
            ynab.TransactionDetailFactory.build(deleted=False, payee_name=payee_name),
        ),
        payee=use_cases.resolve.PayeeResolution(requested=requested, status=use_cases.resolve.PayeeStatus(status)),
    )


def test_bulk_create_json_reports_payee_status_and_merges_fuzzy_flags(runner: CliRunner, container: Container) -> None:
    async def bulk_create(*args: Any, **kwargs: Any) -> AsyncIterator[use_cases.BulkCreateResult]:
        yield _bulk_result("Terra Natura", "created", "Terra Natura")
        yield _bulk_result("supra", "matched", "Supra")

    use_case = MagicMock(wraps=bulk_create)
    container[use_cases.BulkCreate] = use_case

    rows = [
        {"account_name": "Checking", "payee_name": "Terra Natura", "amount_dollars": -1},
        {"account_name": "Checking", "payee_name": "Supra", "amount_dollars": -2, "fuzzy_payee": False},
    ]
    with runner.isolated_filesystem():
        with open("txns.json", "w") as f:
            json.dump(rows, f)
        result = runner.invoke(
            cli, _base_args("bulk-create", "txns.json", "--fuzzy-payee", "--fuzzy-category", json_output=True)
        )

    assert result.exit_code == 0, result.output
    sent = use_case.call_args.args[1]["transactions"]
    # CLI flags are defaults; per-row keys override.
    assert sent[0]["fuzzy_payee"] is True
    assert sent[1]["fuzzy_payee"] is False
    assert sent[0]["fuzzy_category"] is True
    assert sent[0]["fuzzy_account"] is False

    payload = json.loads(result.output)
    assert [(r["payee"], r["payee_status"], r["payee_requested"]) for r in payload] == [
        ("Terra Natura", "created", "Terra Natura"),
        ("supra", "matched", "Supra"),
    ]


def test_bulk_create_table_lists_new_payees(runner: CliRunner, container: Container) -> None:
    async def bulk_create(*args: Any, **kwargs: Any) -> AsyncIterator[use_cases.BulkCreateResult]:
        yield _bulk_result("Terra Natura", "created", "Terra Natura")
        yield _bulk_result("supra", "matched", "supra")

    container[use_cases.BulkCreate] = MagicMock(wraps=bulk_create)

    with runner.isolated_filesystem():
        with open("txns.json", "w") as f:
            json.dump([], f)
        result = runner.invoke(cli, _base_args("bulk-create", "txns.json"))

    assert result.exit_code == 0, result.output
    assert "New payees created: Terra Natura" in result.output
