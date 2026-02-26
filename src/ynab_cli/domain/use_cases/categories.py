import datetime
from collections.abc import AsyncIterator
from typing import TypedDict

from rapidfuzz import process

from ynab_cli.adapters import ynab
from ynab_cli.adapters.ynab import models, util
from ynab_cli.adapters.ynab.api.categories import get_categories, update_month_category
from ynab_cli.adapters.ynab.api.transactions import get_transactions_by_category
from ynab_cli.domain import ports
from ynab_cli.domain.settings import Settings


def _should_skip_category_or_group(category_or_group: models.Category | models.CategoryGroupWithCategories) -> bool:
    if category_or_group.deleted:
        return True
    return False


class ListUnusedParams(TypedDict):
    pass


class ListUnused:
    """Use case for listing unused categories."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ListUnusedParams) -> AsyncIterator[models.Category]:
        try:
            progress_total = 0

            category_groups = (
                await util.get_asyncio_detailed(
                    self._io, get_categories.asyncio_detailed, settings.ynab.budget_id, client=self._client
                )
            ).data.category_groups
            category_groups.sort(key=lambda cg: cg.name)

            progress_total = len(category_groups)
            await self._io.progress.update(total=progress_total)
            for category_group in category_groups:
                await self._io.progress.update(advance=1)

                if _should_skip_category_or_group(category_or_group=category_group):
                    continue

                progress_total += len(category_group.categories)
                await self._io.progress.update(total=progress_total)
                category_group.categories.sort(key=lambda c: c.name)
                for category in category_group.categories:
                    await self._io.progress.update(advance=1)

                    if _should_skip_category_or_group(category_or_group=category):
                        continue

                    transactions = (
                        await util.get_asyncio_detailed(
                            self._io,
                            get_transactions_by_category.asyncio_detailed,
                            settings.ynab.budget_id,
                            str(category.id),
                            client=self._client,
                        )
                    ).data.transactions
                    num_transactions = len(transactions)

                    # List unused category if no transactions
                    if not num_transactions:
                        yield category

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class ListAllParams(TypedDict):
    pass


class ListAll:
    """Use case for listing all categories."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: ListAllParams) -> AsyncIterator[models.Category]:
        try:
            progress_total = 0

            category_groups = (
                await util.get_asyncio_detailed(
                    self._io, get_categories.asyncio_detailed, settings.ynab.budget_id, client=self._client
                )
            ).data.category_groups
            category_groups.sort(key=lambda cg: cg.name)

            progress_total = len(category_groups)
            await self._io.progress.update(total=progress_total)
            for category_group in category_groups:
                await self._io.progress.update(advance=1)

                if _should_skip_category_or_group(category_or_group=category_group):
                    continue

                progress_total += len(category_group.categories)
                await self._io.progress.update(total=progress_total)
                category_group.categories.sort(key=lambda c: c.name)
                for category in category_group.categories:
                    await self._io.progress.update(advance=1)

                    if _should_skip_category_or_group(category_or_group=category):
                        continue

                    yield category

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)


class UpdateBudgetParams(TypedDict, total=False):
    category_name: str
    amount_dollars: float
    month: str | None


class UpdateBudget:
    """Use case for updating a category's budgeted amount for a month."""

    def __init__(self, io: ports.IO, client: ynab.AuthenticatedClient):
        self._io = io
        self._client = client

    async def __call__(self, settings: Settings, params: UpdateBudgetParams) -> AsyncIterator[models.Category]:
        try:
            progress_total = 0

            # Resolve category by fuzzy name
            category_groups = (
                await util.get_asyncio_detailed(
                    self._io, get_categories.asyncio_detailed, settings.ynab.budget_id, client=self._client
                )
            ).data.category_groups
            all_categories = [c for g in category_groups for c in g.categories if not c.deleted and not c.hidden]

            category_name = params["category_name"]
            matched_cat: models.Category | None = None

            # Exact match first
            for c in all_categories:
                if c.name.lower() == category_name.lower():
                    matched_cat = c
                    break

            # Fuzzy fallback
            if not matched_cat:
                names = [c.name for c in all_categories]
                result = process.extractOne(category_name, names, score_cutoff=60)
                if result:
                    matched_name, _score, _idx = result
                    for c in all_categories:
                        if c.name == matched_name:
                            matched_cat = c
                            break

            if not matched_cat:
                await self._io.print(f"Category not found: {category_name}")
                return

            # Determine month
            month_str = params.get("month")
            month_date = (
                datetime.date.fromisoformat(month_str)
                if month_str
                else datetime.datetime.now(tz=datetime.UTC).date().replace(day=1)
            )

            amount_milliunits = round(params["amount_dollars"] * 1000)

            response = await util.get_asyncio_detailed(
                self._io,
                update_month_category.asyncio_detailed,
                settings.ynab.budget_id,
                month_date,
                str(matched_cat.id),
                client=self._client,
                body=models.PatchMonthCategoryWrapper(category=models.SaveMonthCategory(budgeted=amount_milliunits)),
            )

            updated_cat = response.data.category
            progress_total = 1
            await self._io.progress.update(total=progress_total)
            await self._io.progress.update(advance=1)
            yield updated_cat

        except Exception as e:
            if isinstance(e, util.ApiError) and e.status_code == 401:
                await self._io.print("Invalid or expired access token. Please update your settings.")
            elif isinstance(e, util.ApiError) and e.status_code == 429:
                await self._io.print("API rate limit exceeded. Try again later, or get a new access token.")
            else:
                await self._io.print(f"Exception when calling YNAB: {e}")
        finally:
            await self._io.progress.update(total=progress_total, completed=progress_total)
