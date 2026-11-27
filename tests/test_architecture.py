"""Garde-fous d'architecture pour le découpage de main.py.

1. Aucun module (hors main.py) ne doit importer `main` : sous `python main.py`
   le module s'appelle `__main__`, un `import main` le rechargerait une 2e fois
   (2e bot, commandes en double). Les modules passent par `core` (core.bot,
   core.col) ; ce test scanne tous les .py du repo, y compris les futurs.
2. La REST API doit rester portée par le Blueprint `rest_api`.
"""
import ast
import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _python_files():
    files = []
    for pattern in ("*.py", "web/**/*.py", "cogs/**/*.py"):
        files.extend(glob.glob(os.path.join(ROOT, pattern), recursive=True))
    return sorted(f for f in files if os.path.basename(f) != "main.py")


def test_scan_finds_the_modules():
    names = {os.path.relpath(f, ROOT).replace(os.sep, "/") for f in _python_files()}
    assert {"core.py", "audit.py", "web/rest_api.py", "bridge.py"} <= names


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: os.path.relpath(p, ROOT))
def test_module_never_imports_main(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "main" for a in node.names), path
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "main", path


def test_rest_api_is_a_registered_blueprint():
    pytest.importorskip("flask")
    pytest.importorskip("discord")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from web import rest_api
    assert rest_api.bp.name == "rest_api"
    assert callable(rest_api.require_api_key)


WEB_RUNTIME_MODULES = (
    "web/auth.py",
    "web/public.py",
    "web/verify.py",
    "web/github_webhook.py",
    "web/rest_api.py",
    "web/dashboard.py",
)


@pytest.mark.parametrize("relative_path", WEB_RUNTIME_MODULES)
def test_web_routes_do_not_import_bot_or_database_runtime(relative_path):
    """Keep route modules portable; runtime coupling belongs in the adapter."""
    path = os.path.join(ROOT, relative_path)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    forbidden_roots = {"core", "bridge", "bson", "cogs", "discord", "pymongo"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name.split(".")[0] for alias in node.names]
            assert not (forbidden_roots & set(imported)), f"{relative_path}: {imported}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden_roots, f"{relative_path}: {node.module}"


@pytest.mark.parametrize("relative_path", WEB_RUNTIME_MODULES)
def test_web_routes_do_not_bypass_bot_service_adapter(relative_path):
    """Routes must not reach into the in-process adapter or persistence stores."""
    path = os.path.join(ROOT, relative_path)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    forbidden_modules = {
        "web.in_process_bot_service",
        "web.bot_runtime",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name for alias in node.names}
            assert not (forbidden_modules & imported), f"{relative_path}: {imported}"
            assert not any(
                name.rsplit(".", 1)[-1].endswith("_store") for name in imported
            ), f"{relative_path}: {imported}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module not in forbidden_modules, f"{relative_path}: {module}"
            assert not module.rsplit(".", 1)[-1].endswith("_store"), (
                f"{relative_path}: {module}"
            )
