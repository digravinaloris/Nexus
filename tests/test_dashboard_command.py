"""Tests for the Discord dashboard shortcut command."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def test_dashboard_slash_command_exposes_the_web_dashboard_link():
    tree = ast.parse(MAIN_SOURCE)
    command = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "dashboard"
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "command"
            and any(
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "dashboard"
                for keyword in decorator.keywords
            )
            for decorator in node.decorator_list
        )
    )

    command_source = ast.get_source_segment(MAIN_SOURCE, command)
    assert "https://nexus-1qqu.onrender.com/dashboard" in command_source
    assert "discord.ButtonStyle.link" in command_source
    assert "ephemeral=True" in command_source
