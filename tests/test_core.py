"""Tests de core.py : aucun import lourd, aucune vraie base Mongo."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core  # noqa: E402


class FakeCollection:
    def __init__(self, name):
        self.name = name


class FakeDB:
    """Imite `mongo["discordbot"]` : db["x"] renvoie toujours la même collection."""

    def __init__(self):
        self._cols = {}

    def __getitem__(self, name):
        return self._cols.setdefault(name, FakeCollection(name))


@pytest.fixture(autouse=True)
def clean_core():
    core.reset()
    yield
    core.reset()


def test_not_ready_before_init():
    assert core.is_ready() is False
    assert core.col.ready is False
    assert core.db is None and core.mongo is None


def test_access_before_init_raises_clear_error():
    with pytest.raises(core.NotInitialized) as exc:
        core.col.warns
    assert "init_mongo" in str(exc.value)


def test_not_initialized_is_a_runtime_error():
    assert issubclass(core.NotInitialized, RuntimeError)


def test_init_binds_every_declared_collection():
    fake = FakeDB()
    core.init_db("client", fake)
    assert core.is_ready()
    assert core.db is fake and core.mongo == "client"
    for attr, mongo_name in core.COLLECTION_NAMES.items():
        coll = getattr(core.col, attr)
        assert coll.name == mongo_name
        assert coll is fake[mongo_name]  # même objet que db["..."]


def test_special_mongo_names():
    core.init_db(None, FakeDB())
    assert core.col.audit.name == "audit_log"
    assert core.col.scheduled.name == "scheduled_announcements"
    assert core.col.templates.name == "templates"


def test_from_import_col_is_safe_before_init():
    # Le piège que core évite : `from core import col` à l'import d'un module
    # (avant init) doit rester valable APRÈS init.
    from core import col
    with pytest.raises(core.NotInitialized):
        col.config
    fake = FakeDB()
    core.init_db(None, fake)
    assert col.config is fake["config"]


def test_reinit_replaces_collections():
    first, second = FakeDB(), FakeDB()
    core.init_db(None, first)
    assert core.col.warns is first["warns"]
    core.init_db(None, second)  # on_ready peut se redéclencher
    assert core.col.warns is second["warns"]


def test_unknown_collection_is_attribute_error():
    core.init_db(None, FakeDB())
    with pytest.raises(AttributeError):
        core.col.does_not_exist
    with pytest.raises(AttributeError):
        core.col._private


def test_reset_unbinds():
    core.init_db(None, FakeDB())
    core.reset()
    assert core.is_ready() is False
    with pytest.raises(core.NotInitialized):
        core.col.sanctions


def test_collection_names_are_unique():
    names = list(core.COLLECTION_NAMES.values())
    assert len(names) == len(set(names))
    assert "ready" not in core.COLLECTION_NAMES  # ne doit pas masquer la propriété


def test_dir_lists_collections():
    assert "warns" in dir(core.col)
