from typing import cast

from lagom import Container, ExplicitContainer, Singleton

from ynab_cli.domain.constants import YNAB_API_URL


def init_container(container: Container) -> Container:
    from ynab_cli.adapters.ynab.client import AuthenticatedClient
    from ynab_cli.domain.ports.io import IO
    from ynab_cli.domain.settings import Settings
    from ynab_cli.domain.use_cases import accounts as accounts_use_cases
    from ynab_cli.domain.use_cases import budgets as budgets_use_cases
    from ynab_cli.domain.use_cases import categories as categories_use_cases
    from ynab_cli.domain.use_cases import months as months_use_cases
    from ynab_cli.domain.use_cases import payees as payees_use_cases
    from ynab_cli.domain.use_cases import transactions as transactions_use_cases

    #
    # Adapters
    #
    container[AuthenticatedClient] = Singleton(
        lambda c: AuthenticatedClient(
            YNAB_API_URL,
            cast(Settings, c[Settings]).ynab.access_token,
        )
    )

    #
    # Use Cases
    #

    container[accounts_use_cases.ListAll] = lambda c: accounts_use_cases.ListAll(
        c[IO],
        c[AuthenticatedClient],
    )
    container[budgets_use_cases.ListAll] = lambda c: budgets_use_cases.ListAll(
        c[IO],
        c[AuthenticatedClient],
    )
    container[categories_use_cases.ListUnused] = lambda c: categories_use_cases.ListUnused(
        c[IO],
        c[AuthenticatedClient],
    )
    container[categories_use_cases.ListAll] = lambda c: categories_use_cases.ListAll(
        c[IO],
        c[AuthenticatedClient],
    )
    container[categories_use_cases.UpdateBudget] = lambda c: categories_use_cases.UpdateBudget(
        c[IO],
        c[AuthenticatedClient],
    )
    container[months_use_cases.ListAll] = lambda c: months_use_cases.ListAll(
        c[IO],
        c[AuthenticatedClient],
    )
    container[months_use_cases.Show] = lambda c: months_use_cases.Show(
        c[IO],
        c[AuthenticatedClient],
    )
    container[payees_use_cases.NormalizeNames] = lambda c: payees_use_cases.NormalizeNames(
        c[IO],
        c[AuthenticatedClient],
    )
    container[payees_use_cases.ListDuplicates] = lambda c: payees_use_cases.ListDuplicates(
        c[IO],
        c[AuthenticatedClient],
    )
    container[payees_use_cases.ListUnused] = lambda c: payees_use_cases.ListUnused(
        c[IO],
        c[AuthenticatedClient],
    )
    container[payees_use_cases.ListAll] = lambda c: payees_use_cases.ListAll(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.ApplyRules] = lambda c: transactions_use_cases.ApplyRules(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.ListAll] = lambda c: transactions_use_cases.ListAll(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.Create] = lambda c: transactions_use_cases.Create(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.Transfer] = lambda c: transactions_use_cases.Transfer(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.Update] = lambda c: transactions_use_cases.Update(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.Delete] = lambda c: transactions_use_cases.Delete(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.BulkCreate] = lambda c: transactions_use_cases.BulkCreate(
        c[IO],
        c[AuthenticatedClient],
    )
    container[transactions_use_cases.ImportLinked] = lambda c: transactions_use_cases.ImportLinked(
        c[IO],
        c[AuthenticatedClient],
    )

    return container


def make_container() -> Container:  # pragma: no cover
    return init_container(ExplicitContainer())
