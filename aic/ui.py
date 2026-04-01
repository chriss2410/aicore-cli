"""Rich terminal UI helpers with plain-text fallback."""

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    _console = Console()
    RICH = True
except ImportError:
    RICH = False
    _console = None


def print(msg: str, style: str = None):
    if RICH and style:
        _console.print(msg, style=style)
    else:
        __builtins__["print"](msg) if isinstance(__builtins__, dict) else __builtins__.print(msg)


def error(msg: str):
    if RICH:
        _console.print(f"[red]Error:[/red] {msg}")
    else:
        import builtins
        builtins.print(f"Error: {msg}")


def success(msg: str):
    if RICH:
        _console.print(f"[green]✓[/green] {msg}")
    else:
        import builtins
        builtins.print(f"✓ {msg}")


def warning(msg: str):
    if RICH:
        _console.print(f"[yellow]⚠[/yellow] {msg}")
    else:
        import builtins
        builtins.print(f"⚠ {msg}")


def header(title: str):
    if RICH:
        _console.print(Panel(title, style="bold cyan"))
    else:
        import builtins
        builtins.print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def dim(msg: str):
    if RICH:
        _console.print(msg, style="dim")
    else:
        import builtins
        builtins.print(msg)


def prompt(message: str, default: str = None) -> str:
    if RICH:
        return Prompt.ask(message, default=default)
    result = input(f"{message}{f' [{default}]' if default else ''}: ").strip()
    return result if result else default


def confirm(message: str, default: bool = True) -> bool:
    if RICH:
        return Confirm.ask(message, default=default)
    hint = "Y/n" if default else "y/N"
    result = input(f"{message} [{hint}]: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def table(title: str, columns: list[dict], rows: list[list[str]]):
    """Display a table.

    columns: [{"name": "Col", "style": "cyan"}, ...]
    rows: [["val1", "val2"], ...]
    """
    if RICH:
        t = Table(title=title)
        for col in columns:
            t.add_column(col["name"], style=col.get("style"))
        for row in rows:
            t.add_row(*row)
        _console.print(t)
    else:
        import builtins
        builtins.print(f"\n{title}")
        builtins.print("-" * 60)
        header_line = "  ".join(col["name"].ljust(20) for col in columns)
        builtins.print(header_line)
        for row in rows:
            builtins.print("  ".join(str(v).ljust(20) for v in row))


def select(items: list, label: str, display_fn=None) -> int:
    """Show numbered list, return selected index (0-based) or -1 to skip."""
    import builtins

    if not items:
        warning(f"No {label} found.")
        return -1

    builtins.print()
    for i, item in enumerate(items, 1):
        display = display_fn(item) if display_fn else str(item)
        builtins.print(f"  [{i}] {display}")

    while True:
        try:
            choice = prompt(f"Select {label} (1-{len(items)}, 0 to skip)")
            if choice is None or choice == "":
                return -1
            idx = int(choice)
            if idx == 0:
                return -1
            if 1 <= idx <= len(items):
                return idx - 1
            builtins.print(f"  Enter 0-{len(items)}")
        except ValueError:
            builtins.print("  Enter a number")


def banner(title: str, subtitle: str = ""):
    if RICH:
        text = f"[bold cyan]{title}[/bold cyan]"
        if subtitle:
            text += f"\n{subtitle}"
        _console.print(Panel.fit(text, border_style="cyan"))
    else:
        import builtins
        builtins.print(f"\n{'=' * 60}")
        builtins.print(title)
        if subtitle:
            builtins.print(subtitle)
        builtins.print("=" * 60)
