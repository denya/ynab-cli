import datetime
from collections.abc import AsyncIterator
from typing import TypedDict

from ynab_cli.adapters import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.api.months import get_budget_month, get_budget_months
from ynab_cli.domain import ports
from ynab_cli.domain.settings import Settings


class ListAllParams(TypedDict, total=False):
    pass


class ListAll:
    """Use case for listing all budget months."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ListAllParams) -> AsyncIterator[models.MonthSummary]:
        try:
            progress_total = 0

            months = (
                await util.get_asyncio_detailed(
                    self._io, get_budget_months.asyncio_detailed, settings.ynab.budget_id, client=self._client
                )
            ).data.months
            months = [m for m in months if not m.deleted]
            months.sort(key=lambda m: m.month, reverse=True)

            progress_total = len(months)
            await self._io.progress.update(total=progress_total)
            for month in months:
                await self._io.progress.update(advance=1)
                yield month

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class ShowParams(TypedDict):
    month: str


class Show:
    """Use case for showing a single budget month with category details."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ShowParams) -> AsyncIterator[models.Category]:
        try:
            progress_total = 0

            month_date = datetime.date.fromisoformat(params["month"])
            month_detail = (
                await util.get_asyncio_detailed(
                    self._io,
                    get_budget_month.asyncio_detailed,
                    settings.ynab.budget_id,
                    month_date,
                    client=self._client,
                )
            ).data.month

            categories = [c for c in month_detail.categories if not c.deleted and not c.hidden]
            categories.sort(key=lambda c: c.name)

            progress_total = len(categories)
            await self._io.progress.update(total=progress_total)
            for cat in categories:
                await self._io.progress.update(advance=1)
                yield cat

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)
