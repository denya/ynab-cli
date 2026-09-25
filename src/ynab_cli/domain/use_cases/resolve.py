"""Name resolution for accounts, payees and categories.

Resolution is *exact* by default: names are compared case-insensitively after
collapsing whitespace. Fuzzy matching (rapidfuzz WRatio, score >= 60, the
historical behaviour) is only used when explicitly requested by the caller.

For payees, a name with no exact match is treated as a brand-new payee: the
caller should send it as ``payee_name`` so YNAB creates it. Names of the form
``Transfer : <Account>`` are always resolved to the target account's transfer
payee and never create a regular payee.
"""

import enum
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from rapidfuzz import process

from ynab_cli.adapters import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.api.accounts import get_accounts
from ynab_cli.adapters.ynab.api.categories import get_categories
from ynab_cli.adapters.ynab.api.payees import get_payees
from ynab_cli.domain import ports

FUZZY_SCORE_CUTOFF = 60

_TRANSFER_RE = re.compile(r"^\s*transfer\s*:\s*(?P<account>.+?)\s*$", re.IGNORECASE)

T = TypeVar("T")


def normalize_name(name: str) -> str:
    """Normalize a name for exact comparison (case-insensitive, whitespace-collapsed)."""
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class NameMatch(Generic[T]):
    """Result of matching a name against a list of candidates."""

    item: T | None
    fuzzy: bool = False
    score: float | None = None
    suggestion: str | None = None
    """Closest fuzzy candidate when no match was accepted (used for hints only)."""


def match_name(
    name: str,
    candidates: Sequence[T],
    get_name: Callable[[T], str],
    *,
    fuzzy: bool = False,
    fuzzy_candidates: Sequence[T] | None = None,
) -> NameMatch[T]:
    """Match ``name`` exactly against ``candidates``; optionally fall back to fuzzy matching.

    When no match is accepted, ``suggestion`` holds the closest fuzzy candidate (if any)
    so callers can print a helpful hint without silently using it.
    """
    wanted = normalize_name(name)
    for c in candidates:
        if normalize_name(get_name(c)) == wanted:
            return NameMatch(item=c)

    pool = list(fuzzy_candidates if fuzzy_candidates is not None else candidates)
    names = [get_name(c) for c in pool]
    result = process.extractOne(name, names, score_cutoff=FUZZY_SCORE_CUTOFF) if names else None
    if result is None:
        return NameMatch(item=None)

    matched_name, score, idx = result
    if fuzzy:
        return NameMatch(item=pool[idx], fuzzy=True, score=float(score))
    return NameMatch(item=None, suggestion=matched_name)


def not_found_message(kind: str, name: str, match: "NameMatch[Any]", flag: str) -> str:
    msg = f"{kind} not found: {name}"
    if match.suggestion:
        msg += f" (closest: '{match.suggestion}'; use the exact name or pass {flag} to accept fuzzy matches)"
    return msg


async def fetch_accounts(io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str) -> list[models.Account]:
    accounts = (
        await util.get_asyncio_detailed(io, get_accounts.asyncio_detailed, budget_id, client=client)
    ).data.accounts
    return [a for a in accounts if not a.deleted]


async def fetch_payees(io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str) -> list[models.Payee]:
    payees = (await util.get_asyncio_detailed(io, get_payees.asyncio_detailed, budget_id, client=client)).data.payees
    return [p for p in payees if not p.deleted]


async def fetch_categories(io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str) -> list[models.Category]:
    category_groups = (
        await util.get_asyncio_detailed(io, get_categories.asyncio_detailed, budget_id, client=client)
    ).data.category_groups
    return [c for g in category_groups for c in g.categories if not c.deleted and not c.hidden]


def match_account(name: str, accounts: Sequence[models.Account], *, fuzzy: bool = False) -> NameMatch[models.Account]:
    active = [a for a in accounts if not a.deleted and not a.closed]
    return match_name(name, active, lambda a: a.name, fuzzy=fuzzy)


def match_category(
    name: str, categories: Sequence[models.Category], *, fuzzy: bool = False
) -> NameMatch[models.Category]:
    return match_name(name, categories, lambda c: c.name, fuzzy=fuzzy)


class PayeeStatus(enum.StrEnum):
    MATCHED = "matched"
    """Existing payee, exact (case-insensitive) name match."""
    FUZZY_MATCHED = "fuzzy_matched"
    """Existing payee chosen by fuzzy matching (opt-in only)."""
    TRANSFER = "transfer"
    """Transfer payee of another account (``Transfer : <Account>``)."""
    CREATED = "created"
    """No existing payee: ``payee_name`` is sent and YNAB creates a new payee."""


class PayeeResolutionError(Exception):
    pass


@dataclass(frozen=True)
class PayeeResolution:
    requested: str
    status: PayeeStatus
    payee_id: uuid.UUID | None = None
    payee_name: str | None = None
    """Name of the existing payee (None when a new payee will be created)."""
    score: float | None = None


def resolve_payee(
    name: str,
    payees: Sequence[models.Payee],
    accounts: Sequence[models.Account],
    *,
    fuzzy: bool = False,
) -> PayeeResolution:
    """Resolve a payee name.

    1. Case-insensitive exact match against existing payees (includes ``Transfer : X`` payees).
    2. ``Transfer : <Account>`` names resolve to that account's transfer payee; if the account
       doesn't exist a PayeeResolutionError is raised (never silently create such a payee).
    3. If ``fuzzy`` is enabled, fall back to the closest non-transfer payee with score >= 60.
    4. Otherwise the payee is new: status CREATED, caller sends ``payee_name``.
    """
    active = [p for p in payees if not p.deleted]
    exact = match_name(name, active, lambda p: p.name)
    if exact.item is not None:
        p = exact.item
        status = PayeeStatus.TRANSFER if p.transfer_account_id else PayeeStatus.MATCHED
        return PayeeResolution(requested=name, status=status, payee_id=p.id, payee_name=p.name)

    transfer = _TRANSFER_RE.match(name)
    if transfer:
        account_name = transfer.group("account")
        account_match = match_name(account_name, [a for a in accounts if not a.deleted], lambda a: a.name)
        account = account_match.item
        if account is None or account.transfer_payee_id is None:
            raise PayeeResolutionError(
                f"Transfer payee not found: {name} (no account named '{account_name}' with a transfer payee)"
            )
        return PayeeResolution(
            requested=name,
            status=PayeeStatus.TRANSFER,
            payee_id=account.transfer_payee_id,
            payee_name=f"Transfer : {account.name}",
        )

    if fuzzy:
        non_transfer = [p for p in active if not p.transfer_account_id]
        fuzzy_match = match_name(name, [], lambda p: p.name, fuzzy=True, fuzzy_candidates=non_transfer)
        if fuzzy_match.item is not None:
            p = fuzzy_match.item
            return PayeeResolution(
                requested=name,
                status=PayeeStatus.FUZZY_MATCHED,
                payee_id=p.id,
                payee_name=p.name,
                score=fuzzy_match.score,
            )

    return PayeeResolution(requested=name, status=PayeeStatus.CREATED)
