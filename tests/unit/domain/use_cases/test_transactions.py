from http import HTTPStatus
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pytest_mock import MockerFixture

from tests.factories import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.types import Response
from ynab_cli.domain.models import rules
from ynab_cli.domain.settings import Settings
from ynab_cli.domain.use_cases import transactions as use_cases


@pytest.fixture
def transaction_detail() -> models.TransactionDetail:
    return cast(models.TransactionDetail, ynab.TransactionDetailFactory.build())


def test_should_skip_transaction(transaction_detail: models.TransactionDetail) -> None:
    transaction_detail.deleted = False
    assert use_cases._should_skip_transaction(transaction=transaction_detail) is False
    transaction_detail.deleted = True
    assert use_cases._should_skip_transaction(transaction=transaction_detail) is True


def test_get_save_transaction__no_rule_match(transaction_detail: models.TransactionDetail) -> None:
    transaction_rules = rules.TransactionRules.from_dict(
        {
            "transaction_rules": [
                {
                    "rules": ["payee_name == 'Insurance'"],
                    "patch": {
                        "category_id": "00000000-0000-0000-0000-000000000000",
                    },
                },
            ]
        }
    )

    transaction_detail.payee_name = "Not Insurance"
    transaction_detail.category_id = None
    result = use_cases._get_save_transaction(transaction_detail=transaction_detail, transaction_rules=transaction_rules)
    assert result is None


def test_get_save_transaction__match_payee_name(transaction_detail: models.TransactionDetail) -> None:
    transaction_rules = rules.TransactionRules.from_dict(
        {
            "transaction_rules": [
                {
                    "rules": ["payee_name == 'Insurance'"],
                    "patch": {
                        "category_id": "00000000-0000-0000-0000-000000000000",
                    },
                },
            ]
        }
    )

    transaction_detail.payee_name = "Insurance"
    transaction_detail.category_id = None
    result = use_cases._get_save_transaction(transaction_detail=transaction_detail, transaction_rules=transaction_rules)
    assert result is not None
    assert result.to_dict() == {
        "id": transaction_detail.id,
        "category_id": "00000000-0000-0000-0000-000000000000",
        "subtransactions": [],
    }


def test_get_save_transaction__set_split(transaction_detail: models.TransactionDetail) -> None:
    transaction_rules = rules.TransactionRules.from_dict(
        {
            "transaction_rules": [
                {
                    "rules": ["payee_name == 'Insurance'"],
                    "patch": {
                        "category_id": None,
                        "subtransactions": [
                            {
                                "category_id": "00000000-0000-0000-0000-000000000000",
                                "amount": transaction_detail.amount,
                            },
                            {
                                "category_id": "00000000-0000-0000-0000-000000000000",
                                "amount": transaction_detail.amount,
                            },
                        ],
                    },
                },
            ]
        }
    )

    transaction_detail.payee_name = "Insurance"
    transaction_detail.category_id = None
    result = use_cases._get_save_transaction(transaction_detail=transaction_detail, transaction_rules=transaction_rules)
    assert result is not None
    assert result.to_dict() == {
        "id": transaction_detail.id,
        "category_id": None,
        "subtransactions": [
            {"amount": transaction_detail.amount, "category_id": "00000000-0000-0000-0000-000000000000"},
            {"amount": transaction_detail.amount, "category_id": "00000000-0000-0000-0000-000000000000"},
        ],
    }


@pytest.mark.anyio
async def test_apply_rules(mocker: MockerFixture, mock_io: MagicMock) -> None:
    mock_get_transactions = mocker.patch("ynab_cli.domain.use_cases.transactions.get_transactions")
    mock_get_transactions.asyncio_detailed = AsyncMock()
    mock_get_transactions.asyncio_detailed.return_value = Response(
        status_code=HTTPStatus.OK,
        content=b"",
        headers={},
        parsed=models.TransactionsResponse(
            data=models.TransactionsResponseData(
                transactions=[
                    ynab.TransactionDetailFactory.build(payee_name="Insurance", category_id=None, deleted=False),
                    ynab.TransactionDetailFactory.build(deleted=True),
                ],
                server_knowledge=0,
            ),
        ),
    )

    settings = Settings()
    params: use_cases.ApplyRulesParams = {
        "transaction_rules": rules.TransactionRules.from_dict(
            {
                "transaction_rules": [
                    {
                        "rules": ["payee_name == 'Insurance'"],
                        "patch": {
                            "category_id": "00000000-0000-0000-0000-000000000000",
                        },
                    },
                ]
            }
        )
    }

    results: list[tuple[models.TransactionDetail, models.SaveTransactionWithIdOrImportId]] = []
    async for result in use_cases.ApplyRules(mock_io, MagicMock())(settings, params):
        transaction_detail, save_transaction = result
        assert isinstance(transaction_detail, models.TransactionDetail)
        assert isinstance(save_transaction, models.SaveTransactionWithIdOrImportId)
        results.append(result)

    assert len(results) == 1


@pytest.mark.parametrize(
    ("exception", "expected_print"),
    [
        (util.ApiError(401), "Invalid or expired access token. Please update your settings."),
        (util.ApiError(429), "API rate limit exceeded. Try again later, or get a new access token."),
        (Exception("Unexpected error"), "Exception when calling YNAB: Unexpected error"),
    ],
)
@pytest.mark.anyio
async def test_apply_rules_exception(
    exception: Exception, expected_print: str, mocker: MockerFixture, mock_io: MagicMock
) -> None:
    mock_get_transactions = mocker.patch("ynab_cli.domain.use_cases.transactions.get_transactions")
    mock_get_transactions.asyncio_detailed = AsyncMock()
    mock_get_transactions.asyncio_detailed.side_effect = exception

    settings = Settings()
    params: use_cases.ApplyRulesParams = {
        "transaction_rules": rules.TransactionRules.from_dict({"transaction_rules": []})
    }

    async for _ in use_cases.ApplyRules(mock_io, MagicMock())(settings, params):
        pass

    mock_io.print.assert_called_with(expected_print)


@pytest.mark.anyio
async def test_list_all_with_account_filter_forwards_type_param(
    mocker: MockerFixture, mock_io: MagicMock, empty_uuid: UUID
) -> None:
    txn = cast(models.TransactionDetail, ynab.TransactionDetailFactory.build(deleted=False))
    account = models.Account(
        id=empty_uuid,
        name="Checking",
        type_=models.AccountType.CHECKING,
        on_budget=True,
        closed=False,
        balance=0,
        cleared_balance=0,
        uncleared_balance=0,
        transfer_payee_id=None,
        deleted=False,
    )

    mocker.patch(
        "ynab_cli.domain.use_cases.transactions._fuzzy_resolve_account",
        new=AsyncMock(return_value=account),
    )
    mock_get_asyncio_detailed = mocker.patch(
        "ynab_cli.domain.use_cases.transactions.util.get_asyncio_detailed",
        new_callable=AsyncMock,
    )
    mock_get_asyncio_detailed.return_value = models.TransactionsResponse(
        data=models.TransactionsResponseData(
            transactions=[txn],
            server_knowledge=0,
        )
    )

    settings = Settings()
    settings.ynab.budget_id = "budget-id"
    params: use_cases.ListAllParams = {
        "account_name": "checking",
        "type_": "unapproved",
    }

    results = [transaction async for transaction in use_cases.ListAll(mock_io, MagicMock())(settings, params)]
    assert results == [txn]

    assert mock_get_asyncio_detailed.await_count == 1
    assert mock_get_asyncio_detailed.await_args.kwargs["type_"] == models.GetTransactionsByAccountType.UNAPPROVED


@pytest.mark.anyio
async def test_bulk_create_skips_transaction_when_category_is_not_found(
    mocker: MockerFixture, mock_io: MagicMock, empty_uuid: UUID
) -> None:
    account = models.Account(
        id=empty_uuid,
        name="Checking",
        type_=models.AccountType.CHECKING,
        on_budget=True,
        closed=False,
        balance=0,
        cleared_balance=0,
        uncleared_balance=0,
        transfer_payee_id=None,
        deleted=False,
    )
    valid_category = models.Category(
        id=empty_uuid,
        category_group_id=empty_uuid,
        name="Groceries",
        hidden=False,
        budgeted=0,
        activity=0,
        balance=0,
        deleted=False,
    )

    mocker.patch(
        "ynab_cli.domain.use_cases.transactions._fuzzy_resolve_account",
        new=AsyncMock(return_value=account),
    )
    mocker.patch(
        "ynab_cli.domain.use_cases.transactions._fuzzy_resolve_payee",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "ynab_cli.domain.use_cases.transactions._fuzzy_resolve_category",
        new=AsyncMock(side_effect=[None, valid_category]),
    )

    txn = cast(models.TransactionDetail, ynab.TransactionDetailFactory.build(deleted=False))
    mock_get_asyncio_detailed = mocker.patch(
        "ynab_cli.domain.use_cases.transactions.util.get_asyncio_detailed",
        new_callable=AsyncMock,
    )
    mock_get_asyncio_detailed.return_value = models.SaveTransactionsResponse(
        data=models.SaveTransactionsResponseData(
            transaction_ids=[str(txn.id)],
            server_knowledge=0,
            transactions=[txn],
        )
    )

    settings = Settings()
    settings.ynab.budget_id = "budget-id"
    params: use_cases.BulkCreateParams = {
        "transactions": [
            {
                "account_name": "Checking",
                "payee_name": "No Match",
                "amount_dollars": 10.0,
                "category_name": "Missing",
            },
            {
                "account_name": "Checking",
                "payee_name": "No Match",
                "amount_dollars": 20.0,
                "category_name": "Groceries",
            },
        ]
    }

    results = [transaction async for transaction in use_cases.BulkCreate(mock_io, MagicMock())(settings, params)]
    assert results == [txn]

    assert mock_get_asyncio_detailed.await_count == 1
    body = mock_get_asyncio_detailed.await_args.kwargs["body"]
    assert isinstance(body, models.PostTransactionsWrapper)
    assert len(body.transactions) == 1
    assert body.transactions[0].category_id == empty_uuid
    mock_io.print.assert_any_call("Category not found: Missing, skipping for this transaction.")
