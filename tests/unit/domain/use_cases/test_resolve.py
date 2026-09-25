import uuid

import pytest

from ynab_cli.adapters.ynab import models
from ynab_cli.domain.use_cases import resolve

CHECKING_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CHECKING_TRANSFER_PAYEE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SAVINGS_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SAVINGS_TRANSFER_PAYEE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

# Existing payees from the real bug report.
EXISTING_PAYEE_NAMES = [
    "supra",
    "Magma",
    "som mobilitat",
    "Belros",
    "Restaurant El Jardi - Curve",
    "B De Brunch Barcel - Curve",
]


def make_account(
    name: str, account_id: uuid.UUID, transfer_payee_id: uuid.UUID | None, *, closed: bool = False
) -> models.Account:
    return models.Account(
        id=account_id,
        name=name,
        type_=models.AccountType.CHECKING,
        on_budget=True,
        closed=closed,
        balance=0,
        cleared_balance=0,
        uncleared_balance=0,
        transfer_payee_id=transfer_payee_id,
        deleted=False,
    )


def make_payee(name: str, *, transfer_account_id: str | None = None, deleted: bool = False) -> models.Payee:
    return models.Payee(id=uuid.uuid4(), name=name, deleted=deleted, transfer_account_id=transfer_account_id)


def make_category(name: str, *, hidden: bool = False) -> models.Category:
    return models.Category(
        id=uuid.uuid4(),
        category_group_id=uuid.uuid4(),
        name=name,
        hidden=hidden,
        budgeted=0,
        activity=0,
        balance=0,
        deleted=False,
    )


@pytest.fixture
def accounts() -> list[models.Account]:
    return [
        make_account("Checking", CHECKING_ID, CHECKING_TRANSFER_PAYEE_ID),
        make_account("Savings", SAVINGS_ID, SAVINGS_TRANSFER_PAYEE_ID),
    ]


@pytest.fixture
def payees() -> list[models.Payee]:
    return [
        *(make_payee(n) for n in EXISTING_PAYEE_NAMES),
        make_payee("Transfer : Checking", transfer_account_id=str(CHECKING_ID)),
        make_payee("Old Payee", deleted=True),
    ]


NEW_PAYEE_EXAMPLES = [
    ("Terra Natura", "supra"),
    ("Magic Natura", "supra"),
    ("Topmobil", "som mobilitat"),
    ("Idau Sabaters", "Belros"),
    ("Restaurantes Exit", "Restaurant El Jardi - Curve"),
    ("B De Brunch", "B De Brunch Barcel - Curve"),
]


@pytest.mark.parametrize(("name", "_old_wrong_match"), NEW_PAYEE_EXAMPLES)
def test_resolve_payee__new_names_are_created_by_default(
    name: str, _old_wrong_match: str, payees: list[models.Payee], accounts: list[models.Account]
) -> None:
    result = resolve.resolve_payee(name, payees, accounts)
    assert result.status == resolve.PayeeStatus.CREATED
    assert result.payee_id is None
    assert result.requested == name


@pytest.mark.parametrize(("name", "old_wrong_match"), NEW_PAYEE_EXAMPLES)
def test_resolve_payee__fuzzy_is_opt_in_and_keeps_old_behavior(
    name: str, old_wrong_match: str, payees: list[models.Payee], accounts: list[models.Account]
) -> None:
    result = resolve.resolve_payee(name, payees, accounts, fuzzy=True)
    assert result.status == resolve.PayeeStatus.FUZZY_MATCHED
    assert result.payee_name == old_wrong_match
    assert result.score is not None
    assert result.score >= resolve.FUZZY_SCORE_CUTOFF


@pytest.mark.parametrize("name", ["supra", "SUPRA", "  Supra  ", "b de brunch  barcel - curve"])
def test_resolve_payee__case_insensitive_exact_match(
    name: str, payees: list[models.Payee], accounts: list[models.Account]
) -> None:
    result = resolve.resolve_payee(name, payees, accounts)
    assert result.status == resolve.PayeeStatus.MATCHED
    assert result.payee_id is not None


def test_resolve_payee__deleted_payee_is_not_matched(
    payees: list[models.Payee], accounts: list[models.Account]
) -> None:
    assert resolve.resolve_payee("Old Payee", payees, accounts).status == resolve.PayeeStatus.CREATED


def test_resolve_payee__exact_transfer_payee(payees: list[models.Payee], accounts: list[models.Account]) -> None:
    transfer_payee = next(p for p in payees if p.name == "Transfer : Checking")
    result = resolve.resolve_payee("Transfer : Checking", payees, accounts)
    assert result.status == resolve.PayeeStatus.TRANSFER
    assert result.payee_id == transfer_payee.id


@pytest.mark.parametrize("name", ["Transfer : Savings", "transfer: savings", "TRANSFER :Savings"])
def test_resolve_payee__transfer_resolved_via_account(
    name: str, payees: list[models.Payee], accounts: list[models.Account]
) -> None:
    # "Transfer : Savings" isn't in the payee list, but the Savings account has a transfer payee.
    result = resolve.resolve_payee(name, payees, accounts)
    assert result.status == resolve.PayeeStatus.TRANSFER
    assert result.payee_id == SAVINGS_TRANSFER_PAYEE_ID
    assert result.payee_name == "Transfer : Savings"


@pytest.mark.parametrize("fuzzy", [False, True])
def test_resolve_payee__unknown_transfer_account_raises(
    fuzzy: bool, payees: list[models.Payee], accounts: list[models.Account]
) -> None:
    with pytest.raises(resolve.PayeeResolutionError, match="Transfer payee not found"):
        resolve.resolve_payee("Transfer : Savingz", payees, accounts, fuzzy=fuzzy)


def test_resolve_payee__fuzzy_never_picks_transfer_payee(accounts: list[models.Account]) -> None:
    payees = [make_payee("Transfer : Checking", transfer_account_id=str(CHECKING_ID))]
    result = resolve.resolve_payee("Checking Fee", payees, accounts, fuzzy=True)
    assert result.status == resolve.PayeeStatus.CREATED


def test_match_account__exact_by_default_with_suggestion(accounts: list[models.Account]) -> None:
    assert resolve.match_account("checking", accounts).item is accounts[0]

    match = resolve.match_account("Checkin", accounts)
    assert match.item is None
    assert match.suggestion == "Checking"
    msg = resolve.not_found_message("Account", "Checkin", match, "--fuzzy-account")
    assert "Account not found: Checkin" in msg
    assert "closest: 'Checking'" in msg
    assert "--fuzzy-account" in msg


def test_match_account__fuzzy_opt_in(accounts: list[models.Account]) -> None:
    match = resolve.match_account("Checkin", accounts, fuzzy=True)
    assert match.item is accounts[0]
    assert match.fuzzy is True


def test_match_account__closed_accounts_ignored() -> None:
    closed = make_account("Old Card", uuid.uuid4(), None, closed=True)
    assert resolve.match_account("Old Card", [closed]).item is None


def test_match_category__exact_by_default() -> None:
    categories = [make_category("Groceries"), make_category("Dining Out")]
    assert resolve.match_category("groceries", categories).item is categories[0]

    match = resolve.match_category("Grocery", categories)
    assert match.item is None
    assert match.suggestion == "Groceries"

    assert resolve.match_category("Grocery", categories, fuzzy=True).item is categories[0]


def test_match_name__no_candidates() -> None:
    match = resolve.match_name("anything", [], lambda x: str(x), fuzzy=True)
    assert match.item is None
    assert match.suggestion is None
    assert resolve.not_found_message("Account", "anything", match, "--fuzzy-account") == "Account not found: anything"
