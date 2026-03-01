"""
main.py – CLI entry point for AITermsScore.

Usage:
    python main.py score "OpenAI ChatGPT"
    python main.py score "Google Gemini" --vendor google
    python main.py score "Anthropic Claude" --no-html
    python main.py delete-agent          # remove the agent from AI Foundry
"""

from __future__ import annotations

import sys
import click
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

console = Console()


@click.group()
def cli() -> None:
    """AITermsScore – AI product legal document scoring tool."""
    pass


# ──────────────────────────────────────────────────────────────────────────────
# score command
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("product_name")
@click.option("--vendor", default="", help="Vendor name (auto-inferred if omitted).")
@click.option("--timeout", default=300.0, show_default=True,
              help="Max seconds to wait for the agent run.")
@click.option("--no-html", is_flag=True, default=False,
              help="Skip HTML output.")
@click.option("--no-json", is_flag=True, default=False,
              help="Skip JSON output.")
@click.option("--open-html", is_flag=True, default=False,
              help="Open HTML report in default browser when done.")
def score(
    product_name: str,
    vendor: str,
    timeout: float,
    no_html: bool,
    no_json: bool,
    open_html: bool,
) -> None:
    """Score a PRODUCT_NAME against vendor legal documents.

    Example:\n
        python main.py score "OpenAI ChatGPT"
    """
    from config import load_config
    from agent.setup import get_or_create_agent
    from agent.runner import run_scoring
    from output_writer import write_outputs

    console.print(Panel.fit(
        f"[bold cyan]AITermsScore[/bold cyan]\n"
        f"Product: [yellow]{product_name}[/yellow]",
        border_style="cyan",
    ))

    # Load config
    try:
        cfg = load_config()
    except EnvironmentError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        sys.exit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:

        # 1. Connect & provision agent
        task = progress.add_task("Connecting to AI Foundry and provisioning agent…", total=None)
        try:
            client, agent = get_or_create_agent(cfg)
        except Exception as exc:
            console.print(f"[bold red]Agent setup failed:[/bold red] {exc}")
            sys.exit(1)
        progress.update(task, description=f"[green]Agent ready:[/green] {agent.id}")

        # 2. Run the scoring
        task2 = progress.add_task(
            f"Running agent – scoring [bold]{product_name}[/bold]…", total=None
        )
        try:
            result = run_scoring(
                client=client,
                agent=agent,
                product_name=product_name,
                vendor=vendor,
                timeout=timeout,
            )
        except Exception as exc:
            console.print(f"[bold red]Agent run failed:[/bold red] {exc}")
            sys.exit(1)
        progress.update(task2, description="[green]Agent run complete.[/green]")

        # 3. Write outputs
        task3 = progress.add_task("Writing output files…", total=None)
        paths = write_outputs(result, cfg.output_dir)
        progress.update(task3, description="[green]Outputs written.[/green]")

    # Filter formats
    if no_html:
        paths.pop("html", None)
    if no_json:
        paths.pop("json", None)

    # Summary
    console.print("\n[bold green]Scorecard complete![/bold green]")
    for fmt, path in paths.items():
        console.print(f"  [{fmt:8}] {path}")

    # Optionally open HTML
    if open_html and "html" in paths:
        import webbrowser
        webbrowser.open(paths["html"].as_uri())

    # Print structured scores to console
    if result.structured:
        console.print("\n[bold]Score summary:[/bold]")
        _print_scores(result.structured)


def _print_scores(structured: dict) -> None:
    from rich.table import Table

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Criterion", style="dim", min_width=30)
    table.add_column("Score", justify="center")
    table.add_column("Notes")

    for key, val in structured.items():
        if key in ("overall", "metadata"):
            continue
        if isinstance(val, dict):
            score_str = str(val.get("score", "—"))
            notes = str(val.get("notes", ""))
        else:
            score_str = str(val)
            notes = ""
        try:
            score_num = float(score_str)
            if score_num >= 7:
                score_str = f"[green]{score_str}[/green]"
            elif score_num >= 4:
                score_str = f"[yellow]{score_str}[/yellow]"
            else:
                score_str = f"[red]{score_str}[/red]"
        except ValueError:
            pass
        table.add_row(key.replace("_", " ").title(), score_str, notes[:80])

    console.print(table)
    if "overall" in structured:
        console.print(f"\n[bold]Overall score:[/bold] {structured['overall']}")


# ──────────────────────────────────────────────────────────────────────────────
# delete-agent command (utility / cleanup)
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("delete-agent")
@click.confirmation_option(
    prompt="This will delete the agent from Azure AI Foundry. Continue?"
)
def delete_agent_cmd() -> None:
    """Remove the registered agent from Azure AI Foundry."""
    from config import load_config
    from agent.setup import delete_agent

    try:
        cfg = load_config()
        delete_agent(cfg)
        console.print("[green]Agent deleted.[/green]")
    except EnvironmentError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
