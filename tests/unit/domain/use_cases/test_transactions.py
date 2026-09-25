import datetime
from http import HTTPStatus
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pytest_mock import MockerFixture

from tests.factories import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.types import UNSET, Response
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


# --- Name resolution in create / update / bulk-create / transfer -----------------------------------------

CHECKING_ID = UUID("11111111-1111-1111-1111-111111111111")
CHECKING_TRANSFER_PAYEE_ID = UUID("22222222-2222-2222-2222-222222222222")
SAVINGS_ID = UUID("33333333-3333-3333-3333-333333333333")
SAVINGS_TRANSFER_PAYEE_ID = UUID("44444444-4444-4444-4444-444444444444")
SUPRA_ID = UUID("55555555-5555-5555-5555-555555555555")
GROCERIES_ID = UUID("66666666-6666-6666-6666-666666666666")
TERRA_NATURA_NEW_ID = UUID("77777777-7777-7777-7777-777777777777")


def _account(name: str, account_id: UUID, transfer_payee_id: UUID) -> models.Account:
    return models.Account(
        id=account_id,
        name=name,
        type_=models.AccountType.CHECKING,
        on_budget=True,
        closed=False,
        balance=0,
        cleared_balance=0,
        uncleared_balance=0,
        transfer_payee_id=transfer_payee_id,
        deleted=False,
    )


class FakeYnab:
    """Dispatches util.get_asyncio_detailed calls by API function; records write bodies. No network."""

    def __init__(self) -> None:
        self.accounts = [
            _account("Checking", CHECKING_ID, CHECKING_TRANSFER_PAYEE_ID),
            _account("Savings", SAVINGS_ID, SAVINGS_TRANSFER_PAYEE_ID),
        ]
        self.payees = [
            models.Payee(id=SUPRA_ID, name="supra", deleted=False),
            models.Payee(id=UUID(int=8), name="som mobilitat", deleted=False),
            models.Payee(
                id=CHECKING_TRANSFER_PAYEE_ID,
                name="Transfer : Checking",
                deleted=False,
                transfer_account_id=str(CHECKING_ID),
            ),
        ]
        self.categories = [
            models.Category(
                id=GROCERIES_ID,
                category_group_id=UUID(int=9),
                name="Groceries",
                hidden=False,
                budgeted=0,
                activity=0,
                balance=0,
                deleted=False,
            )
        ]
        self.calls: list[str] = []
        self.write_bodies: list[Any] = []

    def _detail_from_new(self, t: models.NewTransaction, idx: int) -> models.TransactionDetail:
        payee_id = t.payee_id if isinstance(t.payee_id, UUID) else TERRA_NATURA_NEW_ID
        return cast(
            models.TransactionDetail,
            ynab.TransactionDetailFactory.build(
                id=f"txn-{idx}",
                account_id=t.account_id,
                date=t.date,
                amount=t.amount,
                payee_id=payee_id,
                payee_name=t.payee_name if isinstance(t.payee_name, str) else "existing",
                deleted=False,
            ),
        )

    async def __call__(self, io: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
        name = fn.__module__.rsplit(".", 1)[-1]
        self.calls.append(name)
        if name == "get_accounts":
            return models.AccountsResponse(data=models.AccountsResponseData(accounts=self.accounts, server_knowledge=0))
        if name == "get_payees":
            return models.PayeesResponse(data=models.PayeesResponseData(payees=self.payees, server_knowledge=0))
        if name == "get_categories":
            group = models.CategoryGroupWithCategories(
                id=UUID(int=9), name="Everyday", hidden=False, deleted=False, categories=self.categories
            )
            return models.CategoriesResponse(
                data=models.CategoriesResponseData(category_groups=[group], server_knowledge=0)
            )
        if name == "create_transaction":
            body = kwargs["body"]
            self.write_bodies.append(body)
            if isinstance(body.transaction, models.NewTransaction):
                txn = self._detail_from_new(body.transaction, 0)
                return models.SaveTransactionsResponse(
                    data=models.SaveTransactionsResponseData(
                        transaction_ids=[txn.id], server_knowledge=0, transaction=txn
                    )
                )
            txns = [self._detail_from_new(t, i) for i, t in enumerate(body.transactions)]
            return models.SaveTransactionsResponse(
                data=models.SaveTransactionsResponseData(
                    transaction_ids=[t.id for t in txns], server_knowledge=0, transactions=txns
                )
            )
        if name == "update_transaction":
            self.write_bodies.append(kwargs["body"])
            return models.TransactionResponse(
                data=models.TransactionResponseData(
                    transaction=ynab.TransactionDetailFactory.build(deleted=False), server_knowledge=0
                )
            )
        raise AssertionError(f"unexpected API call: {name}")


@pytest.fixture
def fake_ynab(mocker: MockerFixture) -> FakeYnab:
    fake = FakeYnab()
    mocker.patch("ynab_cli.adapters.ynab.util.get_asyncio_detailed", new=AsyncMock(side_effect=fake.__call__))
    return fake


@pytest.fixture
def budget_settings() -> Settings:
    settings = Settings()
    settings.ynab.budget_id = "budget-id"
    return settings


@pytest.mark.anyio
async def test_create__new_payee_is_sent_as_payee_name(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.CreateParams = {"account_name": "checking", "payee_name": "Terra Natura", "amount_dollars": -5}
    results = [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)]

    assert len(results) == 1
    body = fake_ynab.write_bodies[0]
    assert body.transaction.payee_name == "Terra Natura"
    assert body.transaction.payee_id is UNSET
    assert body.transaction.account_id == CHECKING_ID


@pytest.mark.anyio
async def test_create__fuzzy_payee_opt_in_uses_existing_payee(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.CreateParams = {
        "account_name": "Checking",
        "payee_name": "Terra Natura",
        "amount_dollars": -5,
        "fuzzy_payee": True,
    }
    _ = [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)]

    body = fake_ynab.write_bodies[0]
    assert body.transaction.payee_id == SUPRA_ID
    assert body.transaction.payee_name is UNSET


@pytest.mark.anyio
async def test_create__exact_payee_is_case_insensitive(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.CreateParams = {"account_name": "Checking", "payee_name": "SUPRA", "amount_dollars": -5}
    _ = [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)]
    assert fake_ynab.write_bodies[0].transaction.payee_id == SUPRA_ID


@pytest.mark.anyio
async def test_create__transfer_payee_keeps_working(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.CreateParams = {
        "account_name": "Savings",
        "payee_name": "Transfer : Checking",
        "amount_dollars": -50,
    }
    _ = [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)]
    txn = fake_ynab.write_bodies[0].transaction
    assert txn.payee_id == CHECKING_TRANSFER_PAYEE_ID
    assert txn.payee_name is UNSET


@pytest.mark.anyio
async def test_create__unknown_transfer_account_does_not_write(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.CreateParams = {"account_name": "Savings", "payee_name": "Transfer : Nope", "amount_dollars": -1}
    results = [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)]
    assert results == []
    assert fake_ynab.write_bodies == []
    assert "Transfer payee not found" in mock_io.print.await_args.args[0]


@pytest.mark.anyio
async def test_create__account_and_category_exact_by_default(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.CreateParams = {"account_name": "Checkin", "payee_name": "supra", "amount_dollars": -1}
    assert [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)] == []
    mock_io.print.assert_awaited_with(
        "Account not found: Checkin (closest: 'Checking'; use the exact name or pass --fuzzy-account "
        "to accept fuzzy matches)"
    )

    params = {"account_name": "Checking", "payee_name": "supra", "amount_dollars": -1, "category_name": "Grocery"}
    assert [t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)] == []
    assert "Category not found: Grocery" in mock_io.print.await_args.args[0]
    assert fake_ynab.write_bodies == []

    params = {
        "account_name": "Checkin",
        "payee_name": "supra",
        "amount_dollars": -1,
        "category_name": "Grocery",
        "fuzzy_account": True,
        "fuzzy_category": True,
    }
    assert len([t async for t in use_cases.Create(mock_io, MagicMock())(budget_settings, params)]) == 1
    txn = fake_ynab.write_bodies[0].transaction
    assert txn.account_id == CHECKING_ID
    assert txn.category_id == GROCERIES_ID


@pytest.mark.anyio
async def test_update__new_payee_is_sent_as_payee_name(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.UpdateParams = {"transaction_id": "t1", "payee_name": "Topmobil"}
    results = [t async for t in use_cases.Update(mock_io, MagicMock())(budget_settings, params)]
    assert len(results) == 1
    existing = fake_ynab.write_bodies[0].transaction
    assert existing.payee_name == "Topmobil"
    assert existing.payee_id is UNSET


@pytest.mark.anyio
async def test_update__fuzzy_payee_opt_in(fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock) -> None:
    params: use_cases.UpdateParams = {"transaction_id": "t1", "payee_name": "Topmobil", "fuzzy_payee": True}
    _ = [t async for t in use_cases.Update(mock_io, MagicMock())(budget_settings, params)]
    assert fake_ynab.write_bodies[0].transaction.payee_id == UUID(int=8)


@pytest.mark.anyio
async def test_update__only_touches_given_fields(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.UpdateParams = {"transaction_id": "t1", "memo": "hi"}
    _ = [t async for t in use_cases.Update(mock_io, MagicMock())(budget_settings, params)]
    assert fake_ynab.calls == ["update_transaction"]


@pytest.mark.anyio
async def test_update__category_exact_by_default(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.UpdateParams = {"transaction_id": "t1", "category_name": "Grocery"}
    assert [t async for t in use_cases.Update(mock_io, MagicMock())(budget_settings, params)] == []
    assert fake_ynab.write_bodies == []


@pytest.mark.anyio
async def test_bulk_create__reports_created_vs_matched_payees(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.BulkCreateParams = {
        "transactions": [
            {"account_name": "Checking", "payee_name": "Terra Natura", "amount_dollars": -1, "date": "2026-09-25"},
            {"account_name": "Checking", "payee_name": "Supra", "amount_dollars": -2, "date": "2026-09-25"},
            {"account_name": "Savings", "payee_name": "Transfer : Checking", "amount_dollars": -3},
            {
                "account_name": "Checking",
                "payee_name": "Topmobil",
                "amount_dollars": -4,
                "fuzzy_payee": True,
            },
        ]
    }
    results = [r async for r in use_cases.BulkCreate(mock_io, MagicMock())(budget_settings, params)]

    statuses = [(r.payee.requested, r.payee.status) for r in results if r.payee]
    assert statuses == [
        ("Terra Natura", use_cases.resolve.PayeeStatus.CREATED),
        ("Supra", use_cases.resolve.PayeeStatus.MATCHED),
        ("Transfer : Checking", use_cases.resolve.PayeeStatus.TRANSFER),
        ("Topmobil", use_cases.resolve.PayeeStatus.FUZZY_MATCHED),
    ]
    body = fake_ynab.write_bodies[0]
    assert [t.payee_name for t in body.transactions] == ["Terra Natura", UNSET, UNSET, UNSET]
    assert body.transactions[3].payee_id == UUID(int=8)
    # Lookups are fetched once per bulk call, not once per row.
    assert fake_ynab.calls.count("get_payees") == 1
    assert fake_ynab.calls.count("get_accounts") == 1
    assert fake_ynab.calls.count("create_transaction") == 1


@pytest.mark.anyio
async def test_bulk_create_skips_transaction_when_category_is_not_found(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.BulkCreateParams = {
        "transactions": [
            {"account_name": "Checking", "payee_name": "No Match", "amount_dollars": 10.0, "category_name": "Missing"},
            {
                "account_name": "Checking",
                "payee_name": "No Match",
                "amount_dollars": 20.0,
                "category_name": "Groceries",
            },
        ]
    }

    results = [r async for r in use_cases.BulkCreate(mock_io, MagicMock())(budget_settings, params)]
    assert len(results) == 1

    body = fake_ynab.write_bodies[0]
    assert isinstance(body, models.PostTransactionsWrapper)
    assert len(body.transactions) == 1
    assert body.transactions[0].category_id == GROCERIES_ID
    mock_io.print.assert_any_call("Category not found: Missing, skipping.")


@pytest.mark.anyio
async def test_bulk_create__nothing_valid_makes_no_write(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.BulkCreateParams = {
        "transactions": [{"account_name": "Nope", "payee_name": "x", "amount_dollars": 1.0}]
    }
    assert [r async for r in use_cases.BulkCreate(mock_io, MagicMock())(budget_settings, params)] == []
    assert fake_ynab.write_bodies == []
    mock_io.print.assert_any_call("No valid transactions to create.")


def test_pair_results__out_of_order_and_ynab_matched_payee() -> None:
    new_a = models.NewTransaction(account_id=CHECKING_ID, date=datetime.date(2026, 9, 25), amount=-1000)
    new_b = models.NewTransaction(account_id=CHECKING_ID, date=datetime.date(2026, 9, 25), amount=-2000)
    res_a = use_cases.resolve.PayeeResolution(requested="A", status=use_cases.resolve.PayeeStatus.CREATED)
    res_b = use_cases.resolve.PayeeResolution(requested="B", status=use_cases.resolve.PayeeStatus.CREATED)
    txn_b = cast(
        models.TransactionDetail,
        ynab.TransactionDetailFactory.build(
            account_id=CHECKING_ID, date=datetime.date(2026, 9, 25), amount=-2000, payee_id=SUPRA_ID
        ),
    )
    txn_a = cast(
        models.TransactionDetail,
        ynab.TransactionDetailFactory.build(
            account_id=CHECKING_ID, date=datetime.date(2026, 9, 25), amount=-1000, payee_id=TERRA_NATURA_NEW_ID
        ),
    )
    results = use_cases._pair_results([txn_b, txn_a], [(new_a, res_a), (new_b, res_b)], {SUPRA_ID})
    assert [r.payee.requested for r in results if r.payee] == ["B", "A"]
    # YNAB resolved "B" to an existing payee id -> reported as matched, not created.
    assert results[0].payee is not None
    assert results[0].payee.status == use_cases.resolve.PayeeStatus.MATCHED
    assert results[1].payee is not None
    assert results[1].payee.status == use_cases.resolve.PayeeStatus.CREATED


@pytest.mark.anyio
async def test_transfer__accounts_exact_by_default(
    fake_ynab: FakeYnab, budget_settings: Settings, mock_io: MagicMock
) -> None:
    params: use_cases.TransferParams = {
        "from_account_name": "Checking",
        "to_account_name": "Saving",
        "amount_dollars": 5,
    }
    assert [t async for t in use_cases.Transfer(mock_io, MagicMock())(budget_settings, params)] == []
    assert mock_io.print.await_args.args[0].startswith("Target account not found: Saving")
    assert fake_ynab.write_bodies == []

    params["fuzzy_account"] = True
    assert len([t async for t in use_cases.Transfer(mock_io, MagicMock())(budget_settings, params)]) == 1
    assert fake_ynab.write_bodies[0].transaction.payee_id == SAVINGS_TRANSFER_PAYEE_ID
