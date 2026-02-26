import datetime
import uuid
from collections.abc import AsyncIterator
from typing import TypedDict

import rule_engine
from rapidfuzz import fuzz, process

from ynab_cli.adapters import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.api.accounts import get_accounts
from ynab_cli.adapters.ynab.api.categories import get_categories
from ynab_cli.adapters.ynab.api.payees import get_payees
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
    accounts = (
        await util.get_asyncio_detailed(io, get_accounts.asyncio_detailed, budget_id, client=client)
    ).data.accounts
    active = [a for a in accounts if not a.deleted and not a.closed]

    for a in active:
        if a.name.lower() == account_name.lower():
            return a

    names = [a.name for a in active]
    result = process.extractOne(account_name, names, score_cutoff=60)
    if result:
        matched_name, _score, _idx = result
        for a in active:
            if a.name == matched_name:
                return a

    return None


async def _fuzzy_resolve_payee(
    io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str, payee_name: str
) -> models.Payee | None:
    payees = (await util.get_asyncio_detailed(io, get_payees.asyncio_detailed, budget_id, client=client)).data.payees
    active = [p for p in payees if not p.deleted]

    for p in active:
        if p.name.lower() == payee_name.lower():
            return p

    names = [p.name for p in active]
    result = process.extractOne(payee_name, names, score_cutoff=60)
    if result:
        matched_name, _score, _idx = result
        for p in active:
            if p.name == matched_name:
                return p

    return None


async def _fuzzy_resolve_category(
    io: ports.IO, client: ynab.AuthenticatedClient, budget_id: str, category_name: str
) -> models.Category | None:
    category_groups = (
        await util.get_asyncio_detailed(io, get_categories.asyncio_detailed, budget_id, client=client)
    ).data.category_groups
    all_categories = [c for g in category_groups for c in g.categories if not c.deleted and not c.hidden]

    for c in all_categories:
        if c.name.lower() == category_name.lower():
            return c

    names = [c.name for c in all_categories]
    result = process.extractOne(category_name, names, score_cutoff=60)
    if result:
        matched_name, _score, _idx = result
        for c in all_categories:
            if c.name == matched_name:
                return c

    return None


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


class Create:
    """Use case for creating a transaction."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: CreateParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            # Resolve account
            account = await _fuzzy_resolve_account(
                self._io, self._client, settings.ynab.budget_id, params["account_name"]
            )
            if not account:
                await self._io.print(f"Account not found: {params['account_name']}")
                return

            # Resolve payee
            payee_id: models.Payee | None = await _fuzzy_resolve_payee(
                self._io, self._client, settings.ynab.budget_id, params["payee_name"]
            )

            # Resolve category
            category_id: None | uuid.UUID | Unset = UNSET
            category_name_param = params.get("category_name")
            if category_name_param:
                category = await _fuzzy_resolve_category(
                    self._io, self._client, settings.ynab.budget_id, category_name_param
                )
                if category:
                    category_id = category.id
                else:
                    await self._io.print(f"Category not found: {category_name_param}")
                    return

            amount_milliunits = round(params["amount_dollars"] * 1000)
            date_str = params.get("date")
            txn_date = (
                datetime.date.fromisoformat(date_str) if date_str else datetime.datetime.now(tz=datetime.UTC).date()
            )
            cleared = (
                models.TransactionClearedStatus.CLEARED
                if params.get("cleared")
                else models.TransactionClearedStatus.UNCLEARED
            )

            new_txn = models.NewTransaction(
                account_id=account.id,
                date=txn_date,
                amount=amount_milliunits,
                payee_id=payee_id.id if payee_id else UNSET,
                payee_name=params["payee_name"] if not payee_id else UNSET,
                category_id=category_id,
                memo=params.get("memo") or UNSET,
                cleared=cleared,
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


class Update:
    """Use case for updating an existing transaction."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: UpdateParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            txn_id = params["transaction_id"]

            # Build ExistingTransaction with only the fields being updated
            existing_txn_kwargs: dict[str, object] = {}

            if params.get("account_name"):
                account = await _fuzzy_resolve_account(
                    self._io,
                    self._client,
                    settings.ynab.budget_id,
                    params["account_name"],  # type: ignore[arg-type]
                )
                if not account:
                    await self._io.print(f"Account not found: {params['account_name']}")
                    return
                existing_txn_kwargs["account_id"] = account.id

            if params.get("payee_name"):
                payee = await _fuzzy_resolve_payee(
                    self._io,
                    self._client,
                    settings.ynab.budget_id,
                    params["payee_name"],  # type: ignore[arg-type]
                )
                if payee:
                    existing_txn_kwargs["payee_id"] = payee.id
                else:
                    existing_txn_kwargs["payee_name"] = params["payee_name"]

            if params.get("category_name"):
                category = await _fuzzy_resolve_category(
                    self._io,
                    self._client,
                    settings.ynab.budget_id,
                    params["category_name"],  # type: ignore[arg-type]
                )
                if not category:
                    await self._io.print(f"Category not found: {params['category_name']}")
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


class BulkCreate:
    """Use case for creating multiple transactions at once."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: BulkCreateParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            new_txns: list[models.NewTransaction] = []
            for txn_params in params["transactions"]:
                # Resolve account
                account = await _fuzzy_resolve_account(
                    self._io, self._client, settings.ynab.budget_id, txn_params["account_name"]
                )
                if not account:
                    await self._io.print(f"Account not found: {txn_params['account_name']}, skipping.")
                    continue

                # Resolve payee
                payee = await _fuzzy_resolve_payee(
                    self._io, self._client, settings.ynab.budget_id, txn_params["payee_name"]
                )

                # Resolve category
                cat_id: None | uuid.UUID | Unset = UNSET
                cat_name = txn_params.get("category_name")
                if cat_name:
                    cat = await _fuzzy_resolve_category(self._io, self._client, settings.ynab.budget_id, cat_name)
                    if cat:
                        cat_id = cat.id
                    else:
                        await self._io.print(f"Category not found: {cat_name}, skipping for this transaction.")
                        continue

                amount_milliunits = round(txn_params["amount_dollars"] * 1000)
                txn_date_str = txn_params.get("date")
                txn_date = (
                    datetime.date.fromisoformat(txn_date_str)
                    if txn_date_str
                    else datetime.datetime.now(tz=datetime.UTC).date()
                )
                cleared = (
                    models.TransactionClearedStatus.CLEARED
                    if txn_params.get("cleared")
                    else models.TransactionClearedStatus.UNCLEARED
                )

                new_txns.append(
                    models.NewTransaction(
                        account_id=account.id,
                        date=txn_date,
                        amount=amount_milliunits,
                        payee_id=payee.id if payee else UNSET,
                        payee_name=txn_params["payee_name"] if not payee else UNSET,
                        category_id=cat_id,
                        memo=txn_params.get("memo") or UNSET,
                        cleared=cleared,
                        approved=True,
                    )
                )

            if not new_txns:
                await self._io.print("No valid transactions to create.")
                return

            response = await util.get_asyncio_detailed(
                self._io,
                create_transaction.asyncio_detailed,
                settings.ynab.budget_id,
                client=self._client,
                body=models.PostTransactionsWrapper(transactions=new_txns),
            )

            created_txns = response.data.transactions
            if isinstance(created_txns, list):
                progress_total = len(created_txns)
                await self._io.progress.update(total=progress_total)
                for txn in created_txns:
                    await self._io.progress.update(advance=1)
                    yield txn
            else:
                await self._io.print(f"Created {len(new_txns)} transactions (no details returned).")

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


class Transfer:
    """Use case for transferring between accounts."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: TransferParams) -> AsyncIterator[models.TransactionDetail]:
        try:
            progress_total = 0

            from_account = await _fuzzy_resolve_account(
                self._io, self._client, settings.ynab.budget_id, params["from_account_name"]
            )
            if not from_account:
                await self._io.print(f"Source account not found: {params['from_account_name']}")
                return

            to_account = await _fuzzy_resolve_account(
                self._io, self._client, settings.ynab.budget_id, params["to_account_name"]
            )
            if not to_account:
                await self._io.print(f"Target account not found: {params['to_account_name']}")
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
