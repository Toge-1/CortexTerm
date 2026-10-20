"""Runtime configuration and an optional, reversible legacy-memory migration."""

from pathlib import Path
import logging

from cortexterm.config import CORTEXTERM_DIR
from cortexterm.memory import MemoryManager
from .model import MemoryModel
from .service import MemoryService


def create_memory_manager(cwd: str, runtime: dict | None) -> MemoryManager:
    manager = MemoryManager(project_root=Path(cwd))
    settings = (runtime or {}).get("memory", {})
    if settings.get("mode", "legacy") == "legacy":
        return manager
    if settings.get("mode") == "off":
        # Keep legacy files untouched while disabling injection and V2 capture.
        manager.get_relevant_context = lambda *args, **kwargs: ""
        return manager
    try:
        model = MemoryModel(runtime, max_calls=int(settings.get("maxExtractionCalls", 30))) if (
            runtime and runtime.get("model") and settings.get("autoExtract", False)
        ) else None
        service = MemoryService(
            Path(cwd), CORTEXTERM_DIR / "memory-v2" / "memory.sqlite3",
            model=model, mode=settings.get("mode", "hybrid"),
            graph=settings.get("graph", False),
            max_tokens=max(100, min(8000, int(settings.get("maxTokens", 2400)))),
        )
        service.import_legacy(manager)
        manager.v2 = service
        service.start()
    except Exception as error:
        logging.getLogger(__name__).warning("Memory V2 unavailable; using legacy (%s)", type(error).__name__)
    return manager
