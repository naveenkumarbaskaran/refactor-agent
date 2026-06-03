"""
CLI entry-point for the refactor-agent.

Usage examples
--------------
# Scan a directory for all smells:
  refactor-agent scan ./src

# Scan for a specific smell:
  refactor-agent scan ./src --smell long-method

# Scan and display in JSON:
  refactor-agent scan ./src --format json

# Apply refactors (dry-run — show diffs but don't write):
  refactor-agent apply ./src --dry-run

# Apply refactors without confirmation prompts:
  refactor-agent apply ./src --yes
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich import print as rprint

from .agent import RefactorAgent
from .smells import SmellDetector

console = Console()

# ---------------------------------------------------------------------------
# Smell-type mapping
# ---------------------------------------------------------------------------

_SMELL_MAP = {
    "all": None,
    "long-method": ["long_method"],
    "nesting": ["deep_nesting"],
    "duplication": ["duplicate_block"],
}

_SMELL_CHOICES = list(_SMELL_MAP.keys())


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="refactor-agent")
def cli() -> None:
    """Refactor Agent — AI-powered Python code smell detector and refactorer."""


# ---------------------------------------------------------------------------
# scan sub-command
# ---------------------------------------------------------------------------

@cli.command("scan")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--smell",
    "smell_type",
    type=click.Choice(_SMELL_CHOICES, case_sensitive=False),
    default="all",
    show_default=True,
    help="Which smell type to detect.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "text"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--long-method-threshold",
    type=int,
    default=20,
    show_default=True,
    help="Lines threshold for long-method detection.",
)
@click.option(
    "--nesting-threshold",
    type=int,
    default=4,
    show_default=True,
    help="Depth threshold for nesting detection.",
)
def scan_cmd(
    path: str,
    smell_type: str,
    output_format: str,
    long_method_threshold: int,
    nesting_threshold: int,
) -> None:
    """Scan PATH for code smells and report them.

    PATH can be a single Python file or a directory (scanned recursively).
    """
    detector = SmellDetector(
        long_method_threshold=long_method_threshold,
        nesting_threshold=nesting_threshold,
    )

    smell_filter = _SMELL_MAP.get(smell_type.lower())
    p = Path(path)

    results = _run_detector(detector, p)

    # Apply smell-type filter.
    if smell_filter:
        results.smells = [s for s in results.smells if s.smell_type in smell_filter]

    if output_format == "json":
        data = [
            {
                "smell_type": s.smell_type,
                "file": s.file_path,
                "line_start": s.line_start,
                "line_end": s.line_end,
                "name": s.name,
                "description": s.description,
                "severity": s.severity,
            }
            for s in results.smells
        ]
        click.echo(json.dumps(data, indent=2))
        return

    if output_format == "text":
        if not results:
            console.print("[green]No code smells detected.[/green]")
        else:
            for smell in results.smells:
                console.print(str(smell))
        return

    # Default: rich table.
    if not results:
        console.print(Panel("[green]No code smells detected.[/green]", title="Scan Result"))
        return

    table = Table(title=f"Code Smells in {path}", show_lines=True)
    table.add_column("Type", style="cyan", no_wrap=True)
    table.add_column("File", style="magenta")
    table.add_column("Lines", style="yellow", justify="right")
    table.add_column("Name", style="green")
    table.add_column("Severity", justify="center")
    table.add_column("Description")

    _SEVERITY_STYLE = {"low": "blue", "medium": "yellow", "high": "red bold"}

    for smell in results.smells:
        sev_style = _SEVERITY_STYLE.get(smell.severity, "white")
        table.add_row(
            smell.smell_type,
            smell.file_path,
            f"{smell.line_start}–{smell.line_end}",
            smell.name or "—",
            f"[{sev_style}]{smell.severity}[/{sev_style}]",
            smell.description,
        )

    console.print(table)
    console.print(
        f"\n[bold]Total:[/bold] {len(results.smells)} smell(s) found."
    )


# ---------------------------------------------------------------------------
# apply sub-command
# ---------------------------------------------------------------------------

@cli.command("apply")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--smell",
    "smell_type",
    type=click.Choice(_SMELL_CHOICES, case_sensitive=False),
    default="all",
    show_default=True,
    help="Which smell type to refactor.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show proposed changes as diffs without writing any files.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Apply all suggested changes without asking for confirmation.",
)
@click.option(
    "--model",
    default=RefactorAgent.MODEL,
    show_default=True,
    help="Claude model to use.",
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    default=None,
    help="Anthropic API key (or set ANTHROPIC_API_KEY env var).",
)
def apply_cmd(
    path: str,
    smell_type: str,
    dry_run: bool,
    yes: bool,
    model: str,
    api_key: Optional[str],
) -> None:
    """Detect smells in PATH and apply AI-suggested refactors.

    Diffs are always printed before any file is written.  Use --dry-run to
    preview changes without modifying any files.
    """
    if dry_run:
        console.print("[yellow]Dry-run mode — no files will be written.[/yellow]")

    smell_filter = _SMELL_MAP.get(smell_type.lower())

    if not api_key:
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[red]Error:[/red] ANTHROPIC_API_KEY not set.  "
            "Pass --api-key or set the environment variable."
        )
        sys.exit(1)

    agent = RefactorAgent(
        api_key=api_key,
        model=model,
        dry_run=dry_run,
        confirm_writes=not yes,
    )

    console.print(f"[bold]Scanning[/bold] {path} for code smells...")
    try:
        result = agent.refactor(path, smell_types=smell_filter)
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    console.print("\n[bold cyan]Agent response:[/bold cyan]")
    console.print(result)

    written = agent.written_files
    if written:
        console.print(f"\n[bold green]Files {'previewed' if dry_run else 'written'}:[/bold green]")
        for f in written:
            console.print(f"  {f}")
    else:
        console.print("\n[dim]No files were modified.[/dim]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_detector(detector: SmellDetector, path: Path):
    from .smells import DetectionResult
    results = DetectionResult()
    if path.is_file():
        results = detector.analyse_file(path)
    elif path.is_dir():
        for py_file in sorted(path.rglob("*.py")):
            file_result = detector.analyse_file(py_file)
            results.smells.extend(file_result.smells)
    return results


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    cli()


if __name__ == "__main__":
    main()
