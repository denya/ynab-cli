import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import TypedDict

import rule_engine
from rapidfuzz import fuzz

from ynab_cli.adapters import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.api.transactions import (
    create_transaction,
    delete_transaction,
    get_transaction_by_id,
    get_transactions,
    get_transactions_by_account,
    import_transactions,
    update_transaction,
    update_transactions,
)
from ynab_cli.adapters.ynab.types import UNSET, Unset
from ynab_cli.domain import ports
from ynab_cli.domain.models import rules
from ynab_cli.domain.settings import Settings
from ynab_cli.domain.use_cases import resolve


def _should_skip_transaction(transaction: models.TransactionDetail) -> bool:
    if transaction.deleted or transaction.category_name in ["Split"]:
        return True
    return False


def _get_save_transaction(
    transaction_detail: models.TransactionDetail,
    transaction_rules: rules.TransactionRules,
) -> models.SaveTransactionWithIdOrImportId | None:
    transaction_detail_dict = transaction_detail.to_dict()

    context = rule_engine.Context(default_value=None)
    for rule in transaction_rules.transaction_rules:
        if any(rule_engine.Rule(rule_str, context=context).matches(transaction_detail_dict) for rule_str in rule.rules):
            if rule.patch:
                save_transaction_dict = rule.patch.to_dict()
                save_transaction_dict["id"] = transaction_detail.id
                save_transaction_dict["import_id"] = transaction_detail.import_id

                save_transaction = models.SaveTransactionWithIdOrImportId.from_dict(save_transaction_dict)

                return save_transaction

    return None


class ApplyRulesParams(TypedDict):
    dry_run: bool
    transaction_rules: rules.TransactionRules


class ApplyRules:
    """Use case for applying rules to transactions."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(
        self, settings: Settings, params: ApplyRulesParams
    ) -> AsyncIterator[tuple[models.TransactionDetail, models.SaveTransactionWithIdOrImportId]]:
        try:
            progress_total = 0

            transactions = (
                await util.get_asyncio_detailed(
                    self._io,
                    get_transactions.asyncio_detailed,
                    settings.ynab.budget_id,
                    client=self._client,
                    type_=models.GetTransactionsType.UNAPPROVED,
                )
            ).data.transactions
            transactions.sort(key=lambda t: t.date)

            save_transactions = []

            progress_total = len(transactions)
            await self._io.progress.update(total=progress_total)
            for transaction in transactions:
                await self._io.progress.update(advance=1)

                if _should_skip_transaction(transaction=transaction):
                    continue

                save_transaction = _get_save_transaction(
                    transaction_detail=transaction, transaction_rules=params["transaction_rules"]
                )
                if save_transaction:
                    yield (transaction, save_transaction)

                    if not params["dry_run"]:
                        save_transactions.append(save_transaction)

            if save_transactions:
                await util.run_asyncio_detailed(
                    self._io,
                    update_transactions.asyncio_detailed,
                    settings.ynab.budget_id,
                    client=self._client,
                    body=models.PatchTransactionsWrapper(transactions=save_transactions),
                )

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


async def _fuzzy_resolve_account(
    io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str, account_name: str
) -> models.Account | None:
    """Resolve an account for read-only filtering (exact first, then fuzzy)."""
    accounts = await resolve.fetch_accounts(io, client, budget_id)
    return resolve.match_account(account_name, accounts, fuzzy=True).item


def _parse_date(date_str: str | None) -> datetime.date:
    return datetime.date.fromisoformat(date_str) if date_str else datetime.datetime.now(tz=datetime.UTC).date()


class _Lookups:
    """Lazily fetched (and cached) budget entities used for name resolution."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str) -> None:
        self._io = io
        self._client = client
        self._budget_id = budget_id
        self._accounts: list[models.Account] | None = None
        self._payees: list[models.Payee] | None = None
        self._categories: list[models.Category] | None = None

    async def accounts(self) -> list[models.Account]:
        if self._accounts is None:
            self._accounts = await resolve.fetch_accounts(self._io, self._client, self._budget_id)
        return self._accounts

    async def payees(self) -> list[models.Payee]:
        if self._payees is None:
            self._payees = await resolve.fetch_payees(self._io, self._client, self._budget_id)
        return self._payees

    async def categories(self) -> list[models.Category]:
        if self._categories is None:
            self._categories = await resolve.fetch_categories(self._io, self._client, self._budget_id)
        return self._categories

    async def account(self, name: str, *, fuzzy: bool) -> tuple[models.Account | None, str | None]:
        match = resolve.match_account(name, await self.accounts(), fuzzy=fuzzy)
        if match.item is None:
            return None, resolve.not_found_message("Account", name, match, "--fuzzy-account")
        return match.item, None

    async def category(self, name: str, *, fuzzy: bool) -> tuple[models.Category | None, str | None]:
        match = resolve.match_category(name, await self.categories(), fuzzy=fuzzy)
        if match.item is None:
            return None, resolve.not_found_message("Category", name, match, "--fuzzy-category")
        return match.item, None

    async def payee(self, name: str, *, fuzzy: bool) -> resolve.PayeeResolution:
        return resolve.resolve_payee(name, await self.payees(), await self.accounts(), fuzzy=fuzzy)


class ListAllParams(TypedDict, total=False):
    since_date: str | None
    account_name: str | None
    payee_filter: str | None
    category_filter: str | None
    type_: str | None
    search_query: str | None


class ListAll:
    """Use case for listing/searching transactions."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ListAllParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            account_name = params.get("account_name")
            since_date_str = params.get("since_date")
            type_str = params.get("type_")
            payee_filter = params.get("payee_filter")
            category_filter = params.get("category_filter")
            search_query = params.get("search_query")

            since_date = datetime.date.fromisoformat(since_date_str) if since_date_str else UNSET
            type_param = models.GetTransactionsType(type_str) if type_str else UNSET
            account_type_param = models.GetTransactionsByAccountType(type_str) if type_str else UNSET

            if account_name:
                account = await _fuzzy_resolve_account(self._io, self._client, settings.ynab.budget_id, account_name)
                if not account:
                    await self._io.print(f"Account not found: {account_name}")
                    return

                transactions = (
                    await util.get_asyncio_detailed(
                        self._io,
                        get_transactions_by_account.asyncio_detailed,
                        settings.ynab.budget_id,
                        str(account.id),
                        client=self._client,
                        since_date=since_date,
                        type_=account_type_param,
                    )
                ).data.transactions
            else:
                transactions = (
                    await util.get_asyncio_detailed(
                        self._io,
                        get_transactions.asyncio_detailed,
                        settings.ynab.budget_id,
                        client=self._client,
                        since_date=since_date,
                        type_=type_param,
                    )
                ).data.transactions

            # Client-side filtering
            filtered = []
            for t in transactions:
                if t.deleted:
                    continue
                if payee_filter and (not t.payee_name or payee_filter.lower() not in str(t.payee_name).lower()):
                    continue
                if category_filter and (
                    not t.category_name or category_filter.lower() not in str(t.category_name).lower()
                ):
                    continue
                if search_query:
                    search_text = f"{t.payee_name or ''} {t.memo or ''}"
                    if fuzz.partial_ratio(search_query.lower(), search_text.lower()) < 60:
                        continue
                filtered.append(t)

            filtered.sort(key=lambda t: t.date, reverse=True)

            progress_total = len(filtered)
            await self._io.progress.update(total=progress_total)
            for transaction in filtered:
                await self._io.progress.update(advance=1)
                yield transaction

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class CreateParams(TypedDict, total=False):
    account_name: str
    payee_name: str
    amount_dollars: float
    category_name: str | None
    memo: str | None
    date: str | None
    cleared: bool
    fuzzy_payee: bool
    """Opt-in: fall back to the closest existing payee (score >= 60) instead of creating a new one."""
    fuzzy_category: bool
    """Opt-in: fall back to the closest category (score >= 60) when there is no exact match."""
    fuzzy_account: bool
    """Opt-in: fall back to the closest account (score >= 60) when there is no exact match."""


async def _build_new_transaction(
    io: ports.IO, lookups: _Lookups, txn_params: CreateParams
) -> tuple[models.NewTransaction, resolve.PayeeResolution] | str:
    """Resolve names and build a NewTransaction. Returns an error message string on failure."""
    account, err = await lookups.account(txn_params["account_name"], fuzzy=bool(txn_params.get("fuzzy_account")))
    if account is None:
        return err or f"Account not found: {txn_params['account_name']}"

    try:
        payee = await lookups.payee(txn_params["payee_name"], fuzzy=bool(txn_params.get("fuzzy_payee")))
    except resolve.PayeeResolutionError as e:
        return str(e)

    category_id: None | uuid.UUID | Unset = UNSET
    category_name = txn_params.get("category_name")
    if category_name:
        category, err = await lookups.category(category_name, fuzzy=bool(txn_params.get("fuzzy_category")))
        if category is None:
            return err or f"Category not found: {category_name}"
        category_id = category.id

    cleared = (
        models.TransactionClearedStatus.CLEARED
        if txn_params.get("cleared")
        else models.TransactionClearedStatus.UNCLEARED
    )
    new_txn = models.NewTransaction(
        account_id=account.id,
        date=_parse_date(txn_params.get("date")),
        amount=round(txn_params["amount_dollars"] * 1000),
        payee_id=payee.payee_id if payee.payee_id else UNSET,
        payee_name=txn_params["payee_name"].strip() if not payee.payee_id else UNSET,
        category_id=category_id,
        memo=txn_params.get("memo") or UNSET,
        cleared=cleared,
        approved=True,
    )
    return new_txn, payee


class Create:
    """Use case for creating a transaction."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: CreateParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            lookups = _Lookups(self._io, self._client, settings.ynab.budget_id)
            built = await _build_new_transaction(self._io, lookups, params)
            if isinstance(built, str):
                await self._io.print(built)
                return
            new_txn, _payee = built

            response = await util.get_asyncio_detailed(
                self._io,
                create_transaction.asyncio_detailed,
                settings.ynab.budget_id,
                client=self._client,
                body=models.PostTransactionsWrapper(transaction=new_txn),
            )

            created_txn = response.data.transaction
            if not isinstance(created_txn, models.TransactionDetail):
                await self._io.print("Transaction created but no details returned.")
                return

            progress_total = 1
            await self._io.progress.update(total=progress_total)
            await self._io.progress.update(advance=1)
            yield created_txn

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class UpdateParams(TypedDict, total=False):
    transaction_id: str
    account_name: str | None
    payee_name: str | None
    amount_dollars: float | None
    category_name: str | None
    memo: str | None
    date: str | None
    cleared: bool | None
    approved: bool | None
    fuzzy_payee: bool
    fuzzy_category: bool
    fuzzy_account: bool


class Update:
    """Use case for updating an existing transaction."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: UpdateParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            txn_id = params["transaction_id"]
            lookups = _Lookups(self._io, self._client, settings.ynab.budget_id)

            # Build ExistingTransaction with only the fields being updated
            existing_txn_kwargs: dict[str, object] = {}

            account_name = params.get("account_name")
            if account_name:
                account, err = await lookups.account(account_name, fuzzy=bool(params.get("fuzzy_account")))
                if account is None:
                    await self._io.print(err or f"Account not found: {account_name}")
                    return
                existing_txn_kwargs["account_id"] = account.id

            payee_name = params.get("payee_name")
            if payee_name:
                try:
                    payee = await lookups.payee(payee_name, fuzzy=bool(params.get("fuzzy_payee")))
                except resolve.PayeeResolutionError as e:
                    await self._io.print(str(e))
                    return
                if payee.payee_id:
                    existing_txn_kwargs["payee_id"] = payee.payee_id
                else:
                    existing_txn_kwargs["payee_name"] = payee_name.strip()

            category_name = params.get("category_name")
            if category_name:
                category, err = await lookups.category(category_name, fuzzy=bool(params.get("fuzzy_category")))
                if category is None:
                    await self._io.print(err or f"Category not found: {category_name}")
                    return
                existing_txn_kwargs["category_id"] = category.id

            if params.get("amount_dollars") is not None:
                existing_txn_kwargs["amount"] = round(params["amount_dollars"] * 1000)  # type: ignore[operator]

            if params.get("memo") is not None:
                existing_txn_kwargs["memo"] = params["memo"]

            if params.get("date"):
                existing_txn_kwargs["date"] = datetime.date.fromisoformat(params["date"])  # type: ignore[arg-type]

            if params.get("cleared") is not None:
                existing_txn_kwargs["cleared"] = (
                    models.TransactionClearedStatus.CLEARED
                    if params["cleared"]
                    else models.TransactionClearedStatus.UNCLEARED
                )

            if params.get("approved") is not None:
                existing_txn_kwargs["approved"] = params["approved"]

            existing_txn = models.ExistingTransaction(**existing_txn_kwargs)  # type: ignore[arg-type]

            response = await util.get_asyncio_detailed(
                self._io,
                update_transaction.asyncio_detailed,
                settings.ynab.budget_id,
                txn_id,
                client=self._client,
                body=models.PutTransactionWrapper(transaction=existing_txn),
            )

            updated_txn = response.data.transaction
            progress_total = 1
            await self._io.progress.update(total=progress_total)
            await self._io.progress.update(advance=1)
            yield updated_txn

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class DeleteParams(TypedDict):
    transaction_id: str


class Delete:
    """Use case for deleting a transaction."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: DeleteParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0
            txn_id = params["transaction_id"]

            # Fetch the transaction first so we can display it
            txn_response = await util.get_asyncio_detailed(
                self._io,
                get_transaction_by_id.asyncio_detailed,
                settings.ynab.budget_id,
                txn_id,
                client=self._client,
            )
            txn = txn_response.data.transaction

            # Delete
            await util.get_asyncio_detailed(
                self._io,
                delete_transaction.asyncio_detailed,
                settings.ynab.budget_id,
                txn_id,
                client=self._client,
            )

            progress_total = 1
            await self._io.progress.update(total=progress_total)
            await self._io.progress.update(advance=1)
            yield txn

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class BulkCreateParams(TypedDict):
    transactions: list[CreateParams]


@dataclass(frozen=True)
class BulkCreateResult:
    transaction: models.TransactionDetail
    payee: resolve.PayeeResolution | None
    """How the payee was resolved (None if the created transaction could not be mapped back to its row)."""


def _pair_results(
    created: list[models.TransactionDetail],
    pending: list[tuple[models.NewTransaction, resolve.PayeeResolution]],
    known_payee_ids: set[uuid.UUID],
) -> list[BulkCreateResult]:
    """Map created transactions back to their input rows (by position, verified by account/date/amount)."""

    def key(account_id: object, date: object, amount: object) -> tuple[str, str, int]:
        return (str(account_id), str(date), int(amount))  # type: ignore[call-overload]

    remaining = list(range(len(pending)))
    results: list[BulkCreateResult] = []
    for pos, txn in enumerate(created):
        txn_key = key(txn.account_id, txn.date, txn.amount)
        idx: int | None = None
        if (
            pos in remaining
            and key(pending[pos][0].account_id, pending[pos][0].date, pending[pos][0].amount) == txn_key
        ):
            idx = pos
        else:
            idx = next(
                (
                    i
                    for i in remaining
                    if key(pending[i][0].account_id, pending[i][0].date, pending[i][0].amount) == txn_key
                ),
                None,
            )
        payee: resolve.PayeeResolution | None = None
        if idx is not None:
            remaining.remove(idx)
            payee = pending[idx][1]
            # YNAB may still match payee_name to an existing payee (e.g. created meanwhile).
            if (
                payee.status == resolve.PayeeStatus.CREATED
                and isinstance(txn.payee_id, uuid.UUID)
                and txn.payee_id in known_payee_ids
            ):
                payee = replace(payee, status=resolve.PayeeStatus.MATCHED, payee_id=txn.payee_id)
        results.append(BulkCreateResult(transaction=txn, payee=payee))
    return results


class BulkCreate:
    """Use case for creating multiple transactions at once.

    Accounts, payees and categories are fetched once per call. Each row may set
    ``fuzzy_payee`` / ``fuzzy_category`` / ``fuzzy_account`` to opt in to fuzzy matching.
    """

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: BulkCreateParams) -> AsyncIterator[BulkCreateResult]:
        try:
            progress_total = 0

            lookups = _Lookups(self._io, self._client, settings.ynab.budget_id)
            pending: list[tuple[models.NewTransaction, resolve.PayeeResolution]] = []
            for txn_params in params["transactions"]:
                built = await _build_new_transaction(self._io, lookups, txn_params)
                if isinstance(built, str):
                    await self._io.print(f"{built}, skipping.")
                    continue
                pending.append(built)

            if not pending:
                await self._io.print("No valid transactions to create.")
                return

            known_payee_ids = {p.id for p in await lookups.payees()}

            response = await util.get_asyncio_detailed(
                self._io,
                create_transaction.asyncio_detailed,
                settings.ynab.budget_id,
                client=self._client,
                body=models.PostTransactionsWrapper(transactions=[t for t, _ in pending]),
            )

            created_txns = response.data.transactions
            if isinstance(created_txns, list):
                progress_total = len(created_txns)
                await self._io.progress.update(total=progress_total)
                for result in _pair_results(created_txns, pending, known_payee_ids):
                    await self._io.progress.update(advance=1)
                    yield result
            else:
                await self._io.print(f"Created {len(pending)} transactions (no details returned).")

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class ImportLinkedParams(TypedDict):
    pass


class ImportLinked:
    """Use case for importing transactions from linked bank accounts."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ImportLinkedParams) -> AsyncIterator[str]:
        try:
            progress_total = 0

            response = await util.get_asyncio_detailed(
                self._io,
                import_transactions.asyncio_detailed,
                settings.ynab.budget_id,
                client=self._client,
            )

            transaction_ids = response.data.transaction_ids
            progress_total = len(transaction_ids)
            await self._io.progress.update(total=progress_total)
            for txn_id in transaction_ids:
                await self._io.progress.update(advance=1)
                yield txn_id

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class TransferParams(TypedDict, total=False):
    from_account_name: str
    to_account_name: str
    amount_dollars: float
    memo: str | None
    date: str | None
    fuzzy_account: bool


class Transfer:
    """Use case for transferring between accounts."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: TransferParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            lookups = _Lookups(self._io, self._client, settings.ynab.budget_id)
            fuzzy_account = bool(params.get("fuzzy_account"))

            from_account, err = await lookups.account(params["from_account_name"], fuzzy=fuzzy_account)
            if not from_account:
                await self._io.print(f"Source {err[0].lower()}{err[1:]}" if err else "Source account not found")
                return

            to_account, err = await lookups.account(params["to_account_name"], fuzzy=fuzzy_account)
            if not to_account:
                await self._io.print(f"Target {err[0].lower()}{err[1:]}" if err else "Target account not found")
                return

            if not to_account.transfer_payee_id:
                await self._io.print(f"Target account '{to_account.name}' does not have a transfer payee ID.")
                return

            amount_milliunits = -abs(round(params["amount_dollars"] * 1000))
            transfer_date_str = params.get("date")
            txn_date = (
                datetime.date.fromisoformat(transfer_date_str)
                if transfer_date_str
                else datetime.datetime.now(tz=datetime.UTC).date()
            )

            new_txn = models.NewTransaction(
                account_id=from_account.id,
                date=txn_date,
                amount=amount_milliunits,
                payee_id=to_account.transfer_payee_id,
                memo=params.get("memo") or UNSET,
                cleared=models.TransactionClearedStatus.CLEARED,
                approved=True,
            )

            response = await util.get_asyncio_detailed(
                self._io,
                create_transaction.asyncio_detailed,
                settings.ynab.budget_id,
                client=self._client,
                body=models.PostTransactionsWrapper(transaction=new_txn),
            )

            created_txn = response.data.transaction
            if not isinstance(created_txn, models.TransactionDetail):
                await self._io.print("Transfer created but no details returned.")
                return

            progress_total = 1
            await self._io.progress.update(total=progress_total)
            await self._io.progress.update(advance=1)
            yield created_txn

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)
