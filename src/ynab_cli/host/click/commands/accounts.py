from typing import Any

import anyio
import click
from lagom import Container

from ynab_cli.domain.settings import Settings
from ynab_cli.domain.use_cases import accounts as use_cases
from ynab_cli.host.click.commands.output import print_json
from ynab_cli.host.click.commands.rich.progress_table import ProgressTable
from ynab_cli.host.click.container import containerize
from ynab_cli.host.constants import CONTEXT_KEY_SETTINGS, ENV_PREFIX


def _format_amount(milliunits: int) -> str:
    dollars = milliunits / 1000
    return f"${dollars:,.2f}"


class ListAllCommand:
    def __init__(self, use_case: use_cases.ListAll, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "All Accounts"
        self._progress_table.table.add_column("Name")
        self._progress_table.table.add_column("Type")
        self._progress_table.table.add_column("Balance", justify="right")
        self._progress_table.table.add_column("Cleared", justify="right")
        self._progress_table.table.add_column("Uncleared", justify="right")

    async def __call__(self, settings: Settings) -> None:
        params: use_cases.ListAllParams = {}

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for account in self._use_case(settings, params):
                row: dict[str, Any] = {
                    "name": account.name,
                    "type": account.type_.value,
                    "balance": account.balance / 1000,
                    "cleared_balance": account.cleared_balance / 1000,
                    "uncleared_balance": account.uncleared_balance / 1000,
                }
                if settings.show_ids:
                    row["id"] = str(account.id)
                rows.append(row)
            print_json(rows)
            return

        if settings.show_ids:
            self._progress_table.table.add_column("ID", no_wrap=True)

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for account in self._use_case(settings, params):
                row_values = [
                    account.name,
                    str(account.type_.value),
                    _format_amount(account.balance),
                    _format_amount(account.cleared_balance),
                    _format_amount(account.uncleared_balance),
                ]
                if settings.show_ids:
                    row_values.append(str(account.id))
                self._progress_table.table.add_row(*row_values)

        if console:
            console.print(self._progress_table.table)


@containerize
async def _list_all(container: Container) -> None:
    await container[ListAllCommand](container[Settings])


@click.command()
@click.pass_context
def list_all(ctx: click.Context) -> None:
    """List all accounts in the YNAB budget.

    JSON output fields: name, type, balance, cleared_balance, uncleared_balance, id (with --show-ids).
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _list_all,
        settings,
        backend_options={"use_uvloop": True},
    )


@click.group()
@click.option("--budget-id", prompt=True, envvar=f"{ENV_PREFIX}_BUDGET_ID", show_envvar=True, help="YNAB budget ID.")
@click.pass_context
def accounts(ctx: click.Context, budget_id: str) -> None:
    """Manage accounts in the YNAB budget."""

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    settings.ynab.budget_id = budget_id
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings


accounts.add_command(list_all)
