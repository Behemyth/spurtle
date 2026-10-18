"""Manifest install command."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from porringer.api import API
    from porringer.schema import SetupActionResult, SyncStrategy


def install(
    manifest: Annotated[
        str,
        typer.Argument(help="Path or URL to a porringer manifest file."),
    ],
    *,
    project_dir: Annotated[
        Path | None,
        typer.Option("--project-dir", help="Project directory override."),
    ] = None,
    strategy: Annotated[
        str,
        typer.Option("--strategy", help="Sync strategy: MINIMAL, LATEST, or EXACT."),
    ] = "MINIMAL",
    prerelease: Annotated[
        list[str] | None,
        typer.Option(
            "--prerelease", help="Package names to allow prerelease versions."
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output results as JSON."),
    ] = False,
) -> None:
    """Install packages from a porringer manifest."""
    from spurtle.cli.context import get_services
    from spurtle.cli.output import render

    _, porringer, _ = get_services()

    # Resolve strategy enum
    from porringer.schema import SyncStrategy

    try:
        sync_strategy = SyncStrategy[strategy.upper()]
    except KeyError:
        typer.echo(
            f"Unknown strategy: {strategy!r}. Use MINIMAL, LATEST, or EXACT.", err=True
        )
        raise typer.Exit(code=1) from None

    prerelease_packages = set(prerelease) if prerelease else None

    try:
        result = asyncio.run(
            _run(
                porringer,
                manifest,
                project_directory=project_dir,
                strategy=sync_strategy,
                prerelease_packages=prerelease_packages,
                json_output=json_output,
            ),
        )
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    render(result, as_json=json_output)


async def _process_install_stream(
    porringer: API,
    manifest_path: Path,
    *,
    project_directory: Path | None,
    strategy: SyncStrategy,
    prerelease_packages: set[str] | None,
    json_output: bool,
) -> tuple[list[SetupActionResult], int]:
    """Run the install stream and collect results."""
    from spurtle.cli.progress import make_install_progress
    from spurtle.operations.install import collect_install

    action_count = 0

    def _on_manifest_loaded(count: int) -> None:
        nonlocal action_count
        action_count = count

    results = await collect_install(
        porringer,
        manifest_path,
        project_directory=project_directory,
        strategy=strategy,
        prerelease_packages=prerelease_packages,
        on_progress=make_install_progress(
            json_output=json_output,
            on_manifest_loaded=_on_manifest_loaded,
        ),
    )

    return list(results.results), action_count


async def _run(
    porringer: API,
    manifest_url: str,
    *,
    project_directory: Path | None,
    strategy: SyncStrategy,
    prerelease_packages: set[str] | None,
    json_output: bool,
) -> dict[str, object]:
    """Execute the install pipeline and return a summary dict."""
    from spurtle.operations.install import open_manifest
    from spurtle.operations.schema import format_install_summary

    async with open_manifest(manifest_url) as manifest_path:
        install_results, action_count = await _process_install_stream(
            porringer,
            manifest_path,
            project_directory=project_directory,
            strategy=strategy,
            prerelease_packages=prerelease_packages,
            json_output=json_output,
        )

        summary = format_install_summary(
            install_results=install_results or None,
        )

        if not json_output:
            typer.echo(summary)

        return {
            "manifest": manifest_url,
            "action_count": action_count,
            "install_succeeded": sum(
                1 for r in install_results if r.success and not r.skipped
            ),
            "install_skipped": sum(1 for r in install_results if r.skipped),
            "install_failed": sum(1 for r in install_results if not r.success),
            "summary": summary,
        }
