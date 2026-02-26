from collections.abc import AsyncIterator
from typing import TypedDict

from ynab_cli.adapters import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.api.accounts import get_accounts
from ynab_cli.domain import ports
from ynab_cli.domain.settings import Settings


class ListAllParams(TypedDict):
    pass


class ListAll:
    """Use case for listing all accounts."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ListAllParams) -> AsyncIterator[models.Account]:
        try:
            progress_total = 0

            accounts = (
                await util.get_asyncio_detailed(
                    self._io, get_accounts.asyncio_detailed, settings.ynab.budget_id, client=self._client
                )
            ).data.accounts
            accounts = [a for a in accounts if not a.deleted and not a.closed]
            accounts.sort(key=lambda a: a.name)

            progress_total = len(accounts)
            await self._io.progress.update(total=progress_total)
            for account in accounts:
                await self._io.progress.update(advance=1)
                yield account

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)
