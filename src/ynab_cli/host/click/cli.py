import logging

import click
from rich.logging import RichHandler

from ynab_cli.domain.settings import Settings
from ynab_cli.host.click.commands import accounts, budgets, categories, months, payees, transactions
from ynab_cli.host.constants import CONTEXT_KEY_DEBUG, CONTEXT_KEY_SETTINGS, ENV_PREFIX


@click.group()
@click.option(
    "--access-token",
    prompt=True,
    hide_input=True,
    envvar=f"{ENV_PREFIX}_ACCESS_TOKEN",
    show_envvar=True,
    help="YNAB API access token.",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    envvar=f"{ENV_PREFIX}_OUTPUT",
    show_envvar=True,
    help="Output format (table for humans, json for LLM/scripts).",
)
@click.option(
    "--show-ids",
    is_flag=True,
    default=False,
    envvar=f"{ENV_PREFIX}_SHOW_IDS",
    show_envvar=True,
    help="Include IDs in output (useful for follow-up commands).",
)
@click.pass_context
def run(
    ctx: click.Context,
    access_token: str,
    output_format: str,
    show_ids: bool,
) -> None:
    """Main entrypoint for YNAB CLI commands.

    \b
    LLM/SCRIPT USAGE:
      Pass --output json --show-ids for compact JSON with entity IDs.
      Set env vars YNAB_CLI_OUTPUT=json and YNAB_CLI_SHOW_IDS=1 to persist.
    \b
    WORKFLOW EXAMPLE:
      1. List accounts:  run --output json accounts list-all
      2. List transactions: run --output json --show-ids transactions list --account "CaixaBank"
      3. Create transfer: run --output json transactions transfer --from "Account A" --to "Account B" --amount 100
         → returns JSON with transaction ID
      4. Update memo:    run transactions update <TRANSACTION_ID> --memo "note"
    \b
    IDS ARE REQUIRED FOR: transactions update, transactions delete.
    IDS COME FROM: --output json on create/transfer (always included),
                   or --show-ids on list commands.
    """

    ctx.ensure_object(dict)
    debug: bool = ctx.obj.get(CONTEXT_KEY_DEBUG, False)
    settings: Settings = ctx.obj.get(CONTEXT_KEY_SETTINGS, Settings())
    settings.ynab.access_token = access_token
    settings.output_format = output_format
    settings.show_ids = show_ids
    ctx.obj[CONTEXT_KEY_SETTINGS] = settings

    logging.basicConfig(
        level="DEBUG" if debug else "WARNING",  # NOTSET, DEBUG, INFO, WARNING, ERROR, CRITICAL
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )


run.add_command(accounts.accounts)
run.add_command(budgets.budgets)
run.add_command(categories.categories)
run.add_command(months.months)
run.add_command(payees.payees)
run.add_command(transactions.transactions)
