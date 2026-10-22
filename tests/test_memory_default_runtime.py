"""Only the product default and migration contract; never call a real model."""
from types import SimpleNamespace

import pytest

from cortexterm import memory as old_memory
from cortexterm.memory_v2 import runtime
from cortexterm.memory_v2.service import MemoryService


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "app"
    monkeypatch.setattr(runtime, "CORTEXTERM_DIR", home)
    monkeypatch.setattr(old_memory, "CORTEXTERM_DIR", home)
    monkeypatch.setattr(runtime, "MemoryModel", lambda config, max_calls: SimpleNamespace(max_calls=max_calls, calls=[]))
    started = []
    monkeypatch.setattr(MemoryService, "start", lambda self: started.append(self))
    return root, started


@pytest.mark.parametrize("old_mode", [None, "legacy", "off", "raw", "hybrid"])
def test_one_default_ignores_retired_mode_settings(configured, old_mode):
    root, started = configured
    settings = {} if old_mode is None else {"mode": old_mode, "autoExtract": False, "autoRecall": True, "graph": True}
    manager = runtime.create_memory_manager(str(root), {"model": "fake", "memory": settings})
    assert manager.v2.model.max_calls == 30
    assert manager.v2.mode == "hybrid"
    assert not manager.v2.auto_recall and not manager.v2.graph
    assert started == [manager.v2]
    assert manager.get_relevant_context(query="anything") == ""


def test_budget_zero_keeps_search_and_import_without_extra_model_calls(configured):
    root, _ = configured
    old = old_memory.MemoryManager(project_root=root)
    old.add_entry(old_memory.MemoryScope.LOCAL, "procedure", "Use python -m pytest")
    source = root / ".cortexterm-memory-local" / "memory.json"
    before = source.read_bytes()
    config = {"model": "fake", "memory": {"maxExtractionCalls": 0}}
    for _ in range(2):
        manager = runtime.create_memory_manager(str(root), config)
        assert manager.v2.model is None
        assert len(manager.v2.search("pytest")["results"]) == 1
        assert source.read_bytes() == before


def test_no_model_still_initializes_history_and_search(configured):
    root, _ = configured
    manager = runtime.create_memory_manager(str(root), None)
    assert manager.v2.model is None
    assert manager.v2.search("notfound")["results"] == []


def test_db_failure_is_reported_without_old_full_context_injection(configured, monkeypatch):
    root, _ = configured
    def unavailable(*args, **kwargs):
        raise OSError("database unavailable")
    monkeypatch.setattr(runtime, "MemoryService", unavailable)
    manager = runtime.create_memory_manager(str(root), {"model": "fake"})
    assert manager.memory_error == "OSError"
    assert manager.get_relevant_context(query="anything") == ""
