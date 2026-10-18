"""Tool and package management commands.

sprt tool check
sprt tool update [plugin] [--package <name>]
sprt tool remove <plugin> <package>
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from spurtle.cli.output import render

tool_app = typer.Typer(
    help="Inspect and manage installed tools and packages.",
    add_completion=False,
)


@tool_app.command("check")
def tool_check(
    *,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON."),
    ] = False,
) -> None:
    """Check for available tool/package updates."""
    from spurtle.cli.context import get_services
    from spurtle.operations.tool import check_tool_updates

    _, porringer, _ = get_services()
    available = asyncio.run(check_tool_updates(porringer))
    render(available, as_json=json_output)


@tool_app.command("update")
def tool_update(
    plugin: Annotated[
        str,
        typer.Argument(help="Installer plugin name to update."),
    ],
    *,
    package: Annotated[
        str | None,
        typer.Option("--package", help="Update a specific package only."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON."),
    ] = False,
) -> None:
    """Update a tool plugin or a specific package within it."""
    from spurtle.cli.context import get_services
    from spurtle.operations.tool import update_tool as _update_tool

    _, porringer, _ = get_services()
    result = asyncio.run(_update_tool(porringer, plugin, package))

    from spurtle.operations.tool import log_update_result

    log_update_result(result)
    render(result, as_json=json_output)


@tool_app.command("remove")
def tool_remove(
    plugin: Annotated[
        str,
        typer.Argument(help="Installer plugin name."),
    ],
    package: Annotated[
        str,
        typer.Argument(help="Package name to remove."),
    ],
    *,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON."),
    ] = False,
) -> None:
    """Remove a single installed package."""
    from spurtle.cli.context import get_services
    from spurtle.operations.tool import remove_package as _remove_package

    _, porringer, _ = get_services()
    success = asyncio.run(_remove_package(porringer, plugin, package))
    render(
        {"success": success, "plugin": plugin, "package": package}, as_json=json_output
    )
