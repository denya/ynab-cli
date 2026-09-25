import json
from collections.abc import Callable
from typing import IO, Any, TypeVar

import anyio
import click
from lagom import Container

from ynab_cli.domain.models import rules
from ynab_cli.domain.settings import Settings
from ynab_cli.domain.use_cases import transactions as use_cases
from ynab_cli.domain.use_cases.resolve import PayeeStatus
from ynab_cli.host.click.commands.output import print_json
from ynab_cli.host.click.commands.rich.progress_table import ProgressTable
from ynab_cli.host.click.container import containerize
from ynab_cli.host.constants import CONTEXT_KEY_SETTINGS, ENV_PREFIX


class ApplyRulesCommand:
    def __init__(self, use_case: use_cases.ApplyRules, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Applying Transaction Rules"
        self._progress_table.table.add_column("Transaction Id")
        self._progress_table.table.add_column("Transaction Date")
        self._progress_table.table.add_column("Transaction Payee")
        self._progress_table.table.add_column("Transaction Category")
        self._progress_table.table.add_column("Transaction Memo")
        self._progress_table.table.add_column("Transaction Amount")
        self._progress_table.table.add_column("Transaction Changes")

    async def __call__(self, settings: Settings, dry_run: bool, transaction_rules: rules.TransactionRules) -> None:
        params: use_cases.ApplyRulesParams = {
            "dry_run": dry_run,
            "transaction_rules": transaction_rules,
        }

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for transaction, save_transaction in self._use_case(settings, params):
                rows.append({
                    "id": transaction.id,
                    "date": transaction.date.isoformat(),
                    "payee": str(transaction.payee_name),
                    "category": str(transaction.category_name),
                    "memo": str(transaction.memo),
                    "amount": transaction.amount / 1000,
                    "changes": save_transaction.to_dict(),
                })
            print_json(rows)
            return

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for transaction, save_transaction in self._use_case(settings, params):
                self._progress_table.table.add_row(
                    transaction.id,
                    transaction.date.isoformat(),
                    str(transaction.payee_name),
                    str(transaction.category_name),
                    str(transaction.memo),
                    str(transaction.amount),
                    str(save_transaction.to_dict()),
                )

        if console:
            console.print(self._progress_table.table)


@containerize
async def _apply_rules(container: Container, dry_run: bool, transaction_rules: rules.TransactionRules) -> None:
    await container[ApplyRulesCommand](container[Settings], dry_run, transaction_rules)


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Run without making any changes.")
@click.argument("rules-file", type=click.File())
@click.pass_context
def apply_rules(ctx: click.Context, dry_run: bool, rules_file: IO[Any]) -> None:
    """Apply transaction rules from a JSON RULES_FILE to transactions in the YNAB budget.

    RULES_FILE should be a JSON file containing transaction rules.
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    transaction_rules = rules.TransactionRules.from_dict(json.load(rules_file))

    anyio.run(
        _apply_rules,
        settings,
        dry_run,
        transaction_rules,
        backend_options={"use_uvloop": True},
    )


@click.group()
@click.option("--budget-id", prompt=True, envvar=f"{ENV_PREFIX}_BUDGET_ID", show_envvar=True, help="YNAB budget ID.")
@click.pass_context
def transactions(ctx: click.Context, budget_id: str) -> None:
    """Manage transactions in the YNAB budget."""

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    settings.ynab.budget_id = budget_id
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings


transactions.add_command(apply_rules)


_FUZZY_HELP = "Opt-in: if there is no exact (case-insensitive) match, use the closest {what} (fuzzy score >= 60)."


_F = TypeVar("_F", bound=Callable[..., Any])


def _fuzzy_options(*, payee: bool = True, category: bool = True, account: bool = True) -> Callable[[_F], _F]:
    """Add --fuzzy-payee/--fuzzy-category/--fuzzy-account and --fuzzy (all of them) options."""

    def decorator(f: _F) -> _F:
        if account:
            f = click.option(
                "--fuzzy-account", is_flag=True, default=False, help=_FUZZY_HELP.format(what="existing account")
            )(f)
        if category:
            f = click.option(
                "--fuzzy-category", is_flag=True, default=False, help=_FUZZY_HELP.format(what="existing category")
            )(f)
        if payee:
            f = click.option(
                "--fuzzy-payee",
                is_flag=True,
                default=False,
                help=_FUZZY_HELP.format(what="existing payee")
                + " Without it, an unknown payee name creates a new payee.",
            )(f)
        return click.option(
            "--fuzzy", is_flag=True, default=False, help="Shortcut for all --fuzzy-* options of this command."
        )(f)

    return decorator


def _fuzzy_flags(
    fuzzy: bool, *, payee: bool = False, category: bool = False, account: bool = False
) -> dict[str, bool]:
    return {
        "fuzzy_payee": fuzzy or payee,
        "fuzzy_category": fuzzy or category,
        "fuzzy_account": fuzzy or account,
    }


def _format_amount(milliunits: int) -> str:
    dollars = milliunits / 1000
    return f"${dollars:,.2f}"


def _txn_to_dict(txn: Any, show_ids: bool = True) -> dict[str, Any]:
    """Convert a transaction to a JSON-friendly dict."""
    row: dict[str, Any] = {
        "date": txn.date.isoformat(),
        "account": str(txn.account_name),
        "payee": str(txn.payee_name or ""),
        "category": str(txn.category_name or ""),
        "memo": str(txn.memo or ""),
        "amount": txn.amount / 1000,
        "cleared": str(txn.cleared.value),
    }
    if show_ids:
        row["id"] = str(txn.id)
    return row


class ListAllCommand:
    def __init__(self, use_case: use_cases.ListAll, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Transactions"
        self._progress_table.table.add_column("Date")
        self._progress_table.table.add_column("Account")
        self._progress_table.table.add_column("Payee")
        self._progress_table.table.add_column("Category")
        self._progress_table.table.add_column("Memo")
        self._progress_table.table.add_column("Amount", justify="right")
        self._progress_table.table.add_column("Cleared")

    async def __call__(
        self,
        settings: Settings,
        since_date: str | None,
        account: str | None,
        payee: str | None,
        category: str | None,
        type_: str | None,
        search: str | None,
    ) -> None:
        params: use_cases.ListAllParams = {
            "since_date": since_date,
            "account_name": account,
            "payee_filter": payee,
            "category_filter": category,
            "type_": type_,
            "search_query": search,
        }

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for txn in self._use_case(settings, params):
                rows.append(_txn_to_dict(txn, show_ids=settings.show_ids))
            print_json(rows)
            return

        if settings.show_ids:
            self._progress_table.table.add_column("ID", no_wrap=True)

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for txn in self._use_case(settings, params):
                row_values = [
                    txn.date.isoformat(),
                    str(txn.account_name),
                    str(txn.payee_name or ""),
                    str(txn.category_name or ""),
                    str(txn.memo or ""),
                    _format_amount(txn.amount),
                    str(txn.cleared.value),
                ]
                if settings.show_ids:
                    row_values.append(str(txn.id))
                self._progress_table.table.add_row(*row_values)

        if console:
            console.print(self._progress_table.table)


@containerize
async def _list_all(
    container: Container,
    since_date: str | None,
    account: str | None,
    payee: str | None,
    category: str | None,
    type_: str | None,
    search: str | None,
) -> None:
    await container[ListAllCommand](container[Settings], since_date, account, payee, category, type_, search)


@click.command("list")
@click.option("--since-date", default=None, help="Only return transactions on or after this date (YYYY-MM-DD).")
@click.option("--account", default=None, help="Filter by account name (fuzzy match).")
@click.option("--payee", default=None, help="Filter by payee name (substring match).")
@click.option("--category", default=None, help="Filter by category name (substring match).")
@click.option(
    "--type",
    "type_",
    default=None,
    type=click.Choice(["unapproved", "uncategorized"], case_sensitive=False),
    help="Filter by transaction type.",
)
@click.option("--search", default=None, help="Fuzzy search across payee and memo.")
@click.pass_context
def list_transactions(
    ctx: click.Context,
    since_date: str | None,
    account: str | None,
    payee: str | None,
    category: str | None,
    type_: str | None,
    search: str | None,
) -> None:
    """List and search transactions in the YNAB budget.

    JSON output fields: date, account, payee, category, memo, amount, cleared, id (with --show-ids).
    Use --show-ids to get transaction IDs needed for update/delete commands.
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _list_all,
        settings,
        since_date,
        account,
        payee,
        category,
        type_,
        search,
        backend_options={"use_uvloop": True},
    )


transactions.add_command(list_transactions)


class CreateCommand:
    def __init__(self, use_case: use_cases.Create, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Created Transaction"
        self._progress_table.table.add_column("Date")
        self._progress_table.table.add_column("Account")
        self._progress_table.table.add_column("Payee")
        self._progress_table.table.add_column("Category")
        self._progress_table.table.add_column("Memo")
        self._progress_table.table.add_column("Amount", justify="right")

    async def __call__(
        self,
        settings: Settings,
        account: str,
        payee: str,
        amount: float,
        category: str | None,
        memo: str | None,
        date: str | None,
        cleared: bool,
        fuzzy: dict[str, bool] | None = None,
    ) -> None:
        params: use_cases.CreateParams = {
            "account_name": account,
            "payee_name": payee,
            "amount_dollars": amount,
            "category_name": category,
            "memo": memo,
            "date": date,
            "cleared": cleared,
            **(fuzzy or {}),  # type: ignore[typeddict-item]
        }

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for txn in self._use_case(settings, params):
                rows.append(_txn_to_dict(txn, show_ids=True))
            print_json(rows)
            return

        if settings.show_ids:
            self._progress_table.table.add_column("ID", no_wrap=True)

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for txn in self._use_case(settings, params):
                row_values = [
                    txn.date.isoformat(),
                    str(txn.account_name),
                    str(txn.payee_name or ""),
                    str(txn.category_name or ""),
                    str(txn.memo or ""),
                    _format_amount(txn.amount),
                ]
                if settings.show_ids:
                    row_values.append(str(txn.id))
                self._progress_table.table.add_row(*row_values)

        if console:
            console.print(self._progress_table.table)


@containerize
async def _create(
    container: Container,
    account: str,
    payee: str,
    amount: float,
    category: str | None,
    memo: str | None,
    date: str | None,
    cleared: bool,
    fuzzy: dict[str, bool],
) -> None:
    await container[CreateCommand](container[Settings], account, payee, amount, category, memo, date, cleared, fuzzy)


@click.command()
@click.option("--account", required=True, help="Account name (exact, case-insensitive).")
@click.option(
    "--payee",
    required=True,
    help="Payee name (exact, case-insensitive; creates a new payee if none matches). "
    "'Transfer : <Account>' creates a transfer.",
)
@click.option("--amount", required=True, type=float, help="Amount in dollars (negative=outflow, positive=inflow).")
@click.option("--category", default=None, help="Category name (exact, case-insensitive).")
@click.option("--memo", default=None, help="Transaction memo.")
@click.option("--date", default=None, help="Transaction date (YYYY-MM-DD, defaults to today).")
@click.option("--cleared", is_flag=True, default=False, help="Mark transaction as cleared.")
@_fuzzy_options()
@click.pass_context
def create(
    ctx: click.Context,
    account: str,
    payee: str,
    amount: float,
    category: str | None,
    memo: str | None,
    date: str | None,
    cleared: bool,
    fuzzy: bool,
    fuzzy_payee: bool,
    fuzzy_category: bool,
    fuzzy_account: bool,
) -> None:
    """Create a new transaction in the YNAB budget.

    Names are matched exactly (case-insensitive). An unknown payee name creates a new
    payee; use --fuzzy-payee to reuse the closest existing payee instead.
    JSON output always includes the transaction ID for follow-up operations.
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _create,
        settings,
        account,
        payee,
        amount,
        category,
        memo,
        date,
        cleared,
        _fuzzy_flags(fuzzy, payee=fuzzy_payee, category=fuzzy_category, account=fuzzy_account),
        backend_options={"use_uvloop": True},
    )


transactions.add_command(create)


class TransferCommand:
    def __init__(self, use_case: use_cases.Transfer, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Transfer"
        self._progress_table.table.add_column("Date")
        self._progress_table.table.add_column("From")
        self._progress_table.table.add_column("To")
        self._progress_table.table.add_column("Amount", justify="right")
        self._progress_table.table.add_column("Memo")

    async def __call__(
        self,
        settings: Settings,
        from_account: str,
        to_account: str,
        amount: float,
        memo: str | None,
        date: str | None,
        fuzzy_account: bool = False,
    ) -> None:
        params: use_cases.TransferParams = {
            "from_account_name": from_account,
            "to_account_name": to_account,
            "amount_dollars": amount,
            "memo": memo,
            "date": date,
            "fuzzy_account": fuzzy_account,
        }

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for txn in self._use_case(settings, params):
                rows.append({
                    "id": str(txn.id),
                    "date": txn.date.isoformat(),
                    "from": str(txn.account_name),
                    "to": str(txn.payee_name or ""),
                    "amount": abs(txn.amount) / 1000,
                    "memo": str(txn.memo or ""),
                })
            print_json(rows)
            return

        if settings.show_ids:
            self._progress_table.table.add_column("ID", no_wrap=True)

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for txn in self._use_case(settings, params):
                row_values = [
                    txn.date.isoformat(),
                    str(txn.account_name),
                    str(txn.payee_name or ""),
                    _format_amount(abs(txn.amount)),
                    str(txn.memo or ""),
                ]
                if settings.show_ids:
                    row_values.append(str(txn.id))
                self._progress_table.table.add_row(*row_values)

        if console:
            console.print(self._progress_table.table)


@containerize
async def _transfer(
    container: Container,
    from_account: str,
    to_account: str,
    amount: float,
    memo: str | None,
    date: str | None,
    fuzzy_account: bool,
) -> None:
    await container[TransferCommand](container[Settings], from_account, to_account, amount, memo, date, fuzzy_account)


@click.command()
@click.option("--from", "from_account", required=True, help="Source account name (exact, case-insensitive).")
@click.option("--to", "to_account", required=True, help="Target account name (exact, case-insensitive).")
@click.option("--amount", required=True, type=float, help="Amount in dollars (positive).")
@click.option("--memo", default=None, help="Transfer memo.")
@click.option("--date", default=None, help="Transfer date (YYYY-MM-DD, defaults to today).")
@_fuzzy_options(payee=False, category=False)
@click.pass_context
def transfer(
    ctx: click.Context,
    from_account: str,
    to_account: str,
    amount: float,
    memo: str | None,
    date: str | None,
    fuzzy: bool,
    fuzzy_account: bool,
) -> None:
    """Transfer between two accounts in the YNAB budget.

    JSON output always includes the transaction ID for follow-up operations (e.g. update memo).
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _transfer,
        settings,
        from_account,
        to_account,
        amount,
        memo,
        date,
        fuzzy or fuzzy_account,
        backend_options={"use_uvloop": True},
    )


transactions.add_command(transfer)


class UpdateCommand:
    def __init__(self, use_case: use_cases.Update, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Updated Transaction"
        self._progress_table.table.add_column("Date")
        self._progress_table.table.add_column("Account")
        self._progress_table.table.add_column("Payee")
        self._progress_table.table.add_column("Category")
        self._progress_table.table.add_column("Memo")
        self._progress_table.table.add_column("Amount", justify="right")
        self._progress_table.table.add_column("Cleared")

    async def __call__(
        self,
        settings: Settings,
        transaction_id: str,
        account: str | None,
        payee: str | None,
        amount: float | None,
        category: str | None,
        memo: str | None,
        date: str | None,
        cleared: bool | None,
        approved: bool | None,
        fuzzy: dict[str, bool] | None = None,
    ) -> None:
        params: use_cases.UpdateParams = {
            "transaction_id": transaction_id,
            "account_name": account,
            "payee_name": payee,
            "amount_dollars": amount,
            "category_name": category,
            "memo": memo,
            "date": date,
            "cleared": cleared,
            "approved": approved,
            **(fuzzy or {}),  # type: ignore[typeddict-item]
        }

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for txn in self._use_case(settings, params):
                rows.append(_txn_to_dict(txn, show_ids=True))
            print_json(rows)
            return

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for txn in self._use_case(settings, params):
                self._progress_table.table.add_row(
                    txn.date.isoformat(),
                    str(txn.account_name),
                    str(txn.payee_name or ""),
                    str(txn.category_name or ""),
                    str(txn.memo or ""),
                    _format_amount(txn.amount),
                    str(txn.cleared.value),
                )

        if console:
            console.print(self._progress_table.table)


@containerize
async def _update(
    container: Container,
    transaction_id: str,
    account: str | None,
    payee: str | None,
    amount: float | None,
    category: str | None,
    memo: str | None,
    date: str | None,
    cleared: bool | None,
    approved: bool | None,
    fuzzy: dict[str, bool],
) -> None:
    await container[UpdateCommand](
        container[Settings], transaction_id, account, payee, amount, category, memo, date, cleared, approved, fuzzy
    )


@click.command()
@click.argument("transaction-id")
@click.option("--account", default=None, help="New account name (exact, case-insensitive).")
@click.option(
    "--payee",
    default=None,
    help="New payee name (exact, case-insensitive; creates a new payee if none matches). "
    "'Transfer : <Account>' makes it a transfer.",
)
@click.option("--amount", default=None, type=float, help="New amount in dollars.")
@click.option("--category", default=None, help="New category name (exact, case-insensitive).")
@click.option("--memo", default=None, help="New memo.")
@click.option("--date", default=None, help="New date (YYYY-MM-DD).")
@click.option("--cleared/--uncleared", default=None, help="Set cleared status.")
@click.option("--approved/--unapproved", default=None, help="Set approved status.")
@_fuzzy_options()
@click.pass_context
def update(
    ctx: click.Context,
    transaction_id: str,
    account: str | None,
    payee: str | None,
    amount: float | None,
    category: str | None,
    memo: str | None,
    date: str | None,
    cleared: bool | None,
    approved: bool | None,
    fuzzy: bool,
    fuzzy_payee: bool,
    fuzzy_category: bool,
    fuzzy_account: bool,
) -> None:
    """Update an existing transaction by TRANSACTION_ID.

    Get the TRANSACTION_ID from 'list --show-ids' or '--output json' on create/transfer commands.
    JSON output always includes the updated transaction ID.
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _update,
        settings,
        transaction_id,
        account,
        payee,
        amount,
        category,
        memo,
        date,
        cleared,
        approved,
        _fuzzy_flags(fuzzy, payee=fuzzy_payee, category=fuzzy_category, account=fuzzy_account),
        backend_options={"use_uvloop": True},
    )


transactions.add_command(update)


class DeleteCommand:
    def __init__(self, use_case: use_cases.Delete, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Deleted Transaction"
        self._progress_table.table.add_column("Date")
        self._progress_table.table.add_column("Account")
        self._progress_table.table.add_column("Payee")
        self._progress_table.table.add_column("Category")
        self._progress_table.table.add_column("Memo")
        self._progress_table.table.add_column("Amount", justify="right")

    async def __call__(self, settings: Settings, transaction_id: str) -> None:
        params: use_cases.DeleteParams = {"transaction_id": transaction_id}

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for txn in self._use_case(settings, params):
                row: dict[str, Any] = {
                    "id": str(txn.id),
                    "date": txn.date.isoformat(),
                    "account": str(txn.account_name),
                    "payee": str(txn.payee_name or ""),
                    "category": str(txn.category_name or ""),
                    "memo": str(txn.memo or ""),
                    "amount": txn.amount / 1000,
                }
                rows.append(row)
            print_json(rows)
            return

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for txn in self._use_case(settings, params):
                self._progress_table.table.add_row(
                    txn.date.isoformat(),
                    str(txn.account_name),
                    str(txn.payee_name or ""),
                    str(txn.category_name or ""),
                    str(txn.memo or ""),
                    _format_amount(txn.amount),
                )

        if console:
            console.print(self._progress_table.table)


@containerize
async def _delete(container: Container, transaction_id: str) -> None:
    await container[DeleteCommand](container[Settings], transaction_id)


@click.command("delete")
@click.argument("transaction-id")
@click.confirmation_option(prompt="Are you sure you want to delete this transaction?")
@click.pass_context
def delete_cmd(ctx: click.Context, transaction_id: str) -> None:
    """Delete a transaction by TRANSACTION_ID (requires confirmation)."""

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _delete,
        settings,
        transaction_id,
        backend_options={"use_uvloop": True},
    )


transactions.add_command(delete_cmd)


class BulkCreateCommand:
    def __init__(self, use_case: use_cases.BulkCreate, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Bulk Created Transactions"
        self._progress_table.table.add_column("Date")
        self._progress_table.table.add_column("Account")
        self._progress_table.table.add_column("Payee")
        self._progress_table.table.add_column("Payee Status")
        self._progress_table.table.add_column("Category")
        self._progress_table.table.add_column("Memo")
        self._progress_table.table.add_column("Amount", justify="right")

    async def __call__(self, settings: Settings, transactions_data: list[use_cases.CreateParams]) -> None:
        params: use_cases.BulkCreateParams = {"transactions": transactions_data}

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for result in self._use_case(settings, params):
                row = _txn_to_dict(result.transaction, show_ids=True)
                if result.payee is not None:
                    row["payee_status"] = result.payee.status.value
                    row["payee_requested"] = result.payee.requested
                rows.append(row)
            print_json(rows)
            return

        if settings.show_ids:
            self._progress_table.table.add_column("ID", no_wrap=True)

        created: list[str] = []
        fuzzy_matched: list[str] = []
        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for result in self._use_case(settings, params):
                txn = result.transaction
                status = result.payee.status.value if result.payee else ""
                if result.payee and result.payee.status == PayeeStatus.CREATED:
                    created.append(result.payee.requested)
                if result.payee and result.payee.status == PayeeStatus.FUZZY_MATCHED:
                    fuzzy_matched.append(f"{result.payee.requested} -> {result.payee.payee_name}")
                row_values = [
                    txn.date.isoformat(),
                    str(txn.account_name),
                    str(txn.payee_name or ""),
                    status,
                    str(txn.category_name or ""),
                    str(txn.memo or ""),
                    _format_amount(txn.amount),
                ]
                if settings.show_ids:
                    row_values.append(str(txn.id))
                self._progress_table.table.add_row(*row_values)

        if console:
            console.print(self._progress_table.table)
            if created:
                console.print("New payees created: " + ", ".join(dict.fromkeys(created)))
            if fuzzy_matched:
                console.print("Fuzzy-matched payees: " + ", ".join(dict.fromkeys(fuzzy_matched)))


@containerize
async def _bulk_create(container: Container, transactions_json: str, fuzzy: dict[str, bool]) -> None:
    import json

    transactions_data: list[use_cases.CreateParams] = json.loads(transactions_json)
    # Command-line flags are defaults; per-row fuzzy_* keys override them.
    transactions_data = [{**fuzzy, **row} for row in transactions_data]  # type: ignore[typeddict-item]
    await container[BulkCreateCommand](container[Settings], transactions_data)


@click.command("bulk-create")
@click.argument("transactions-file", type=click.File())
@_fuzzy_options()
@click.pass_context
def bulk_create(
    ctx: click.Context,
    transactions_file: Any,
    fuzzy: bool,
    fuzzy_payee: bool,
    fuzzy_category: bool,
    fuzzy_account: bool,
) -> None:
    """Bulk create transactions from a JSON TRANSACTIONS_FILE.

    The file should contain a JSON array of objects with keys:
    account_name, payee_name, amount_dollars, and optionally
    category_name, memo, date (YYYY-MM-DD), cleared (bool), and
    fuzzy_payee / fuzzy_category / fuzzy_account (bool, per-row override
    of the --fuzzy-* flags).

    Names are matched exactly (case-insensitive); unknown payee names create
    new payees. Output reports each row's payee_status: created, matched,
    fuzzy_matched or transfer. JSON output always includes transaction IDs.
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _bulk_create,
        settings,
        transactions_file.read(),
        _fuzzy_flags(fuzzy, payee=fuzzy_payee, category=fuzzy_category, account=fuzzy_account),
        backend_options={"use_uvloop": True},
    )


transactions.add_command(bulk_create)


class ImportLinkedCommand:
    def __init__(self, use_case: use_cases.ImportLinked, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Imported Transactions"
        self._progress_table.table.add_column("Transaction ID")

    async def __call__(self, settings: Settings) -> None:
        params: use_cases.ImportLinkedParams = {}

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for txn_id in self._use_case(settings, params):
                rows.append({"id": txn_id})
            print_json(rows)
            return

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for txn_id in self._use_case(settings, params):
                self._progress_table.table.add_row(txn_id)

        if console:
            console.print(self._progress_table.table)


@containerize
async def _import_linked(container: Container) -> None:
    await container[ImportLinkedCommand](container[Settings])


@click.command("import-linked")
@click.pass_context
def import_linked(ctx: click.Context) -> None:
    """Import transactions from linked bank accounts."""

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _import_linked,
        settings,
        backend_options={"use_uvloop": True},
    )


transactions.add_command(import_linked)
