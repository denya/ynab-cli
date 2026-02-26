from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, Concatenate, ParamSpec, TypeVar

from lagom import Container

from ynab_cli.domain.settings import Settings
from ynab_cli.host.container import make_container as host_make_container

R = TypeVar("R")
P = ParamSpec("P")


def init_container(container: Container) -> Container:
    from rich import table as rich_table

    from ynab_cli.adapters.rich.io import RichIO
    from ynab_cli.domain.ports.io import IO
    from ynab_cli.domain.use_cases import accounts as accounts_use_cases
    from ynab_cli.domain.use_cases import budgets as budgets_use_cases
    from ynab_cli.domain.use_cases import categories as categories_use_cases
    from ynab_cli.domain.use_cases import months as months_use_cases
    from ynab_cli.domain.use_cases import payees as payees_use_cases
    from ynab_cli.domain.use_cases import transactions as transactions_use_cases
    from ynab_cli.host.click.commands import accounts as accounts_commands
    from ynab_cli.host.click.commands import budgets as budgets_commands
    from ynab_cli.host.click.commands import categories as categories_commands
    from ynab_cli.host.click.commands import months as months_commands
    from ynab_cli.host.click.commands import payees as payees_commands
    from ynab_cli.host.click.commands import transactions as transactions_commands
    from ynab_cli.host.click.commands.rich.progress_table import ProgressTable

    #
    # Host
    #
    container[ProgressTable] = ProgressTable(rich_table.Table())
    container[accounts_commands.ListAllCommand] = lambda c: accounts_commands.ListAllCommand(
        c[accounts_use_cases.ListAll],
        c[ProgressTable],
    )
    container[budgets_commands.ListAllCommand] = lambda c: budgets_commands.ListAllCommand(
        c[budgets_use_cases.ListAll],
        c[ProgressTable],
    )
    container[categories_commands.ListUnusedCommand] = lambda c: categories_commands.ListUnusedCommand(
        c[categories_use_cases.ListUnused],
        c[ProgressTable],
    )
    container[categories_commands.ListAllCommand] = lambda c: categories_commands.ListAllCommand(
        c[categories_use_cases.ListAll],
        c[ProgressTable],
    )
    container[categories_commands.UpdateBudgetCommand] = lambda c: categories_commands.UpdateBudgetCommand(
        c[categories_use_cases.UpdateBudget],
        c[ProgressTable],
    )
    container[months_commands.ListAllCommand] = lambda c: months_commands.ListAllCommand(
        c[months_use_cases.ListAll],
        c[ProgressTable],
    )
    container[months_commands.ShowCommand] = lambda c: months_commands.ShowCommand(
        c[months_use_cases.Show],
        c[ProgressTable],
    )
    container[payees_commands.NormalizeNamesCommand] = lambda c: payees_commands.NormalizeNamesCommand(
        c[payees_use_cases.NormalizeNames],
        c[ProgressTable],
    )
    container[payees_commands.ListDuplicatesCommand] = lambda c: payees_commands.ListDuplicatesCommand(
        c[payees_use_cases.ListDuplicates],
        c[ProgressTable],
    )
    container[payees_commands.ListUnusedCommand] = lambda c: payees_commands.ListUnusedCommand(
        c[payees_use_cases.ListUnused],
        c[ProgressTable],
    )
    container[payees_commands.ListAllCommand] = lambda c: payees_commands.ListAllCommand(
        c[payees_use_cases.ListAll],
        c[ProgressTable],
    )
    container[transactions_commands.ApplyRulesCommand] = lambda c: transactions_commands.ApplyRulesCommand(
        c[transactions_use_cases.ApplyRules],
        c[ProgressTable],
    )
    container[transactions_commands.ListAllCommand] = lambda c: transactions_commands.ListAllCommand(
        c[transactions_use_cases.ListAll],
        c[ProgressTable],
    )
    container[transactions_commands.CreateCommand] = lambda c: transactions_commands.CreateCommand(
        c[transactions_use_cases.Create],
        c[ProgressTable],
    )
    container[transactions_commands.TransferCommand] = lambda c: transactions_commands.TransferCommand(
        c[transactions_use_cases.Transfer],
        c[ProgressTable],
    )
    container[transactions_commands.UpdateCommand] = lambda c: transactions_commands.UpdateCommand(
        c[transactions_use_cases.Update],
        c[ProgressTable],
    )
    container[transactions_commands.DeleteCommand] = lambda c: transactions_commands.DeleteCommand(
        c[transactions_use_cases.Delete],
        c[ProgressTable],
    )
    container[transactions_commands.BulkCreateCommand] = lambda c: transactions_commands.BulkCreateCommand(
        c[transactions_use_cases.BulkCreate],
        c[ProgressTable],
    )
    container[transactions_commands.ImportLinkedCommand] = lambda c: transactions_commands.ImportLinkedCommand(
        c[transactions_use_cases.ImportLinked],
        c[ProgressTable],
    )

    #
    # Adapters
    #
    container[IO] = lambda c: RichIO(c[ProgressTable])  # type: ignore[type-abstract]

    return container


def make_container() -> Container:  # pragma: no cover
    return init_container(host_make_container())


def containerize(
    func: Callable[Concatenate[Container, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[Settings, P], Coroutine[Any, Any, R]]:
    @wraps(func)
    async def wrapper(settings: Settings, *args: P.args, **kwargs: P.kwargs) -> R:
        container = make_container()
        container[Settings] = settings

        return await func(container, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
