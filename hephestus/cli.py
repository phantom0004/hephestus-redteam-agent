import os
import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from hephestus import config

console = Console()

BANNER_ART = r"""  ___ ________________________  ___ ___    _____  ___________ ________________________ ___  _________
 /   |   \_   _____/\______   \/   |   \  /  _  \ \_   _____//   _____/\__    ___/    |   \/   _____/
/    ~    \    __)_  |     ___/    ~    \/  /_\  \ |    __)_ \_____  \   |    |  |    |   /\_____  \ 
\    Y    /        \ |    |   \    Y    /    |    \|        \/        \  |    |  |    |  / /        \
 \___|_  /_______  / |____|    \___|_  /\____|__  /_______  /_______  /  |____|  |______/ /_______  /
       \/        \/                  \/         \/        \/        \/                            \/ """

BANNER_TAGLINE = "------- Intelligence is my element"


def render_banner() -> None:
    """Render the project banner."""
    art_text = Text(BANNER_ART, style="bold deep_sky_blue3")
    tagline_text = Text(BANNER_TAGLINE, style="bold bright_white")
    console.print(
        Panel.fit(
            Text.assemble(art_text, "\n\n", tagline_text),
            border_style="bright_magenta",
            padding=(1, 2),
            title="[b]Hephestus Agent[/b]",
        )
    )


@click.command()
@click.argument("goal", required=False)
@click.option("--context", "-c", help="Additional context for the task.")
@click.option(
    "--config",
    "config_path",
    help="Path to YAML config file.",
    type=click.Path(dir_okay=False),
)
@click.option(
    "--dry-run", is_flag=True, help="Simulate execution without running commands."
)
@click.option(
    "--log-dir",
    help="Directory to save logs.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option("--max-tokens", help="LLM max tokens.", type=int)
@click.option("--model", help="LLM model.")
@click.option("--max-steps", help="Maximum iterations.", type=int)
@click.option("--timeout", help="Overall timeout in seconds.", type=int)
@click.option("--backend", help="Execution backend (default: local).")
@click.option(
    "--validate-network",
    is_flag=True,
    help="Observe host connectivity; this does not prove VM isolation.",
)
def main(
    goal,
    context,
    config_path,
    dry_run,
    log_dir,
    max_tokens,
    model,
    max_steps,
    timeout,
    backend,
    validate_network,
):
    """
    Hephestus: A supervised local LLM research agent.

    GOAL: The objective for the agent (quoted string).
    """
    render_banner()

    if not goal:
        ctx = click.get_current_context()
        click.echo(ctx.get_help())
        return

    # Input validation
    goal_stripped = goal.strip()
    if not goal_stripped or len(goal_stripped) < 5:
        console.print(
            "[bold red]Error:[/bold red] Goal must be at least 5 characters long."
        )
        sys.exit(1)
    if len(goal_stripped) > 5000:
        console.print(
            "[bold red]Error:[/bold red] Goal must not exceed 5000 characters."
        )
        sys.exit(1)
    if context and len(context) > 10000:
        console.print(
            "[bold red]Error:[/bold red] Context must not exceed 10000 characters."
        )
        sys.exit(1)

    if config_path and not os.path.isfile(config_path):
        console.print(
            f"[bold red]Error:[/bold red] Config file not found: {config_path}"
        )
        sys.exit(1)

    config.load(
        config_path=config_path,
        overrides={
            "dry_run": dry_run if dry_run else None,
            "log_dir": log_dir,
            "max_tokens": max_tokens,
            "max_steps": max_steps,
            "timeout_seconds": timeout,
            "llm_model": model,
            "backend": backend,
        },
    )

    if config.CONFIG_PATH and not os.path.isfile(config.CONFIG_PATH):
        console.print(
            f"[bold red]Error:[/bold red] Config file not found: {config.CONFIG_PATH}"
        )
        sys.exit(1)

    if config.CONFIG_ERRORS:
        console.print("[bold red]Error:[/bold red] Invalid configuration values:")
        for error in config.CONFIG_ERRORS:
            console.print(f" - {error}")
        sys.exit(1)

    if config.DRY_RUN:
        console.print(
            "[yellow]Running in DRY-RUN mode. No commands will be executed.[/yellow]"
        )

    from hephestus.src.local_llm import validate_runtime

    try:
        validate_runtime(config.LLM_MODEL)
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    # Network isolation validation
    if validate_network:
        from hephestus.utils.network_validator import validate_isolation

        result = validate_isolation()
        console.print(result["reason"], markup=False)
        for check in result["checks"]:
            console.print(
                f"{check['check']}: {check['status']} - {check['detail']}", markup=False
            )
        console.print("These checks do not enforce network isolation.", markup=False)

    _ensure_log_dir(config.LOG_DIR)
    console.print(f"[dim]Using local model: {config.LLM_MODEL}[/dim]")
    console.print(f"[dim]Log directory: {config.LOG_DIR}[/dim]")

    from hephestus.src import executor

    try:
        executor.set_backend_by_name(config.BACKEND)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        console.print(
            f"Available backends: {', '.join(executor.available_backends().keys())}"
        )
        sys.exit(1)

    # Launch Agent
    from hephestus.src import agent

    try:
        agent.run(goal=goal_stripped, context=context or "")
    except KeyboardInterrupt:
        console.print("\n[bold red]Aborted by user.[/bold red]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Fatal Error:[/bold red] {e}")
        sys.exit(1)


def _ensure_log_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
        test_path = os.path.join(path, ".hephestus_write_test")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
    except Exception as exc:
        console.print(
            f"[bold red]Error:[/bold red] Log directory is not writable: {path}"
        )
        console.print(f"[dim]{exc}[/dim]")
        sys.exit(1)


if __name__ == "__main__":
    main()
