"""One product memory policy: durable history, bounded extraction and on-demand recall."""

from pathlib import Path
import logging

from cortexterm.config import CORTEXTERM_DIR
from cortexterm.memory import MemoryManager
from .model import MemoryModel
from .service import MemoryService


def create_memory_manager(cwd: str, runtime: dict | None) -> MemoryManager:
    manager = MemoryManager(project_root=Path(cwd))
    settings = (runtime or {}).get("memory", {})
    if any(key in settings for key in ("mode", "autoExtract", "autoRecall", "graph")):
        logging.getLogger(__name__).info("Old memory strategy settings are ignored; using unified memory")
    try:
        budget = max(0, min(100, int(settings.get("maxExtractionCalls", 30))))
        model = MemoryModel(runtime, max_calls=budget) if (
            runtime and runtime.get("model") and budget
        ) else None
        service = MemoryService(
            Path(cwd), CORTEXTERM_DIR / "memory-v2" / "memory.sqlite3",
            model=model,
            auto_recall=False,
            max_tokens=max(100, min(8000, int(settings.get("maxTokens", 2400)))),
        )
        service.import_legacy(manager)
        manager.v2 = service
        service.start()
    except Exception as error:
        # Do not silently switch to an older injection policy when the store fails.
        manager.memory_error = type(error).__name__
        manager.get_relevant_context = lambda *args, **kwargs: ""
        logging.getLogger(__name__).warning("Memory initialization failed (%s)", type(error).__name__)
    return manager
