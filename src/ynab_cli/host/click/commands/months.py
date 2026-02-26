from typing import Any

import anyio
import click
from lagom import Container

from ynab_cli.domain.settings import Settings
from ynab_cli.domain.use_cases import months as use_cases
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

        self._progress_table.table.title = "Budget Months"
        self._progress_table.table.add_column("Month")
        self._progress_table.table.add_column("Income", justify="right")
        self._progress_table.table.add_column("Budgeted", justify="right")
        self._progress_table.table.add_column("Activity", justify="right")
        self._progress_table.table.add_column("To Be Budgeted", justify="right")
        self._progress_table.table.add_column("Age of Money")

    async def __call__(self, settings: Settings) -> None:
        params: use_cases.ListAllParams = {}

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for month in self._use_case(settings, params):
                rows.append({
                    "month": month.month.isoformat(),
                    "income": month.income / 1000,
                    "budgeted": month.budgeted / 1000,
                    "activity": month.activity / 1000,
                    "to_be_budgeted": month.to_be_budgeted / 1000,
                    "age_of_money": month.age_of_money,
                })
            print_json(rows)
            return

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for month in self._use_case(settings, params):
                age = str(month.age_of_money) if month.age_of_money is not None else ""
                self._progress_table.table.add_row(
                    month.month.isoformat(),
                    _format_amount(month.income),
                    _format_amount(month.budgeted),
                    _format_amount(month.activity),
                    _format_amount(month.to_be_budgeted),
                    age,
                )

        if console:
            console.print(self._progress_table.table)


@containerize
async def _list_all(container: Container) -> None:
    await container[ListAllCommand](container[Settings])


@click.command()
@click.pass_context
def list_all(ctx: click.Context) -> None:
    """List all budget months.

    JSON output fields: month, income, budgeted, activity, to_be_budgeted, age_of_money.
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _list_all,
        settings,
        backend_options={"use_uvloop": True},
    )


class ShowCommand:
    def __init__(self, use_case: use_cases.Show, progress_table: ProgressTable) -> None:
        self._use_case = use_case
        self._progress_table = progress_table

        self._progress_table.table.title = "Budget Month Categories"
        self._progress_table.table.add_column("Category")
        self._progress_table.table.add_column("Budgeted", justify="right")
        self._progress_table.table.add_column("Activity", justify="right")
        self._progress_table.table.add_column("Balance", justify="right")

    async def __call__(self, settings: Settings, month: str) -> None:
        params: use_cases.ShowParams = {"month": month}

        if settings.output_format == "json":
            rows: list[dict[str, Any]] = []
            async for cat in self._use_case(settings, params):
                row: dict[str, Any] = {
                    "name": cat.name,
                    "budgeted": cat.budgeted / 1000,
                    "activity": cat.activity / 1000,
                    "balance": cat.balance / 1000,
                }
                if settings.show_ids:
                    row["id"] = str(cat.id)
                rows.append(row)
            print_json(rows)
            return

        console = None
        with self._progress_table:
            console = self._progress_table.console

            async for cat in self._use_case(settings, params):
                self._progress_table.table.add_row(
                    cat.name,
                    _format_amount(cat.budgeted),
                    _format_amount(cat.activity),
                    _format_amount(cat.balance),
                )

        if console:
            console.print(self._progress_table.table)


@containerize
async def _show(container: Container, month: str) -> None:
    await container[ShowCommand](container[Settings], month)


@click.command()
@click.argument("month")
@click.pass_context
def show(ctx: click.Context, month: str) -> None:
    """Show category details for a budget MONTH (YYYY-MM-DD, first of month).

    JSON output fields: name, budgeted, activity, balance, id (with --show-ids).
    """

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    anyio.run(
        _show,
        settings,
        month,
        backend_options={"use_uvloop": True},
    )


@click.group()
@click.option("--budget-id", prompt=True, envvar=f"{ENV_PREFIX}_BUDGET_ID", show_envvar=True, help="YNAB budget ID.")
@click.pass_context
def months(ctx: click.Context, budget_id: str) -> None:
    """Manage budget months in the YNAB budget."""

    ctx.ensure_object(dict)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    settings.ynab.budget_id = budget_id
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings


months.add_command(list_all)
months.add_command(show)
