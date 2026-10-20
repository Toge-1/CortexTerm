"""Inspect and process Memory V2 without starting the terminal UI."""

import argparse
import json
from pathlib import Path

from cortexterm.config import CORTEXTERM_DIR, load_runtime_config
from .model import MemoryModel
from .service import MemoryService


def main():
    parser = argparse.ArgumentParser(description="CortexTerm scoped memory diagnostics")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path, default=CORTEXTERM_DIR / "memory-v2" / "memory.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show local queue and visible record counts; no model calls")
    search = commands.add_parser("search", help="Show evidence, ranking channels and time filter")
    search.add_argument("query")
    search.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
    search.add_argument("--graph", action="store_true")
    read = commands.add_parser("read", help="Read a scoped memory or complete source turn")
    read.add_argument("id")
    process = commands.add_parser("process", help="Explicitly run queued extraction with real model calls")
    process.add_argument("--max-calls", type=int, default=5)
    process.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    service = MemoryService(args.project, args.db)
    if args.command == "status":
        result = service.store.stats(service.project, service.workspace)
    elif args.command == "search":
        service.graph = args.graph
        result = service.search(args.query, mode=args.mode)
    elif args.command == "read":
        result = service.read(args.id)
    else:
        if not 1 <= args.max_calls <= 100:
            parser.error("--max-calls must be between 1 and 100")
        service.model = MemoryModel(load_runtime_config(args.project), max_calls=args.max_calls)
        if args.retry_failed:
            service.store.retry_failed(service.project, service.workspace)
        for _ in range(args.max_calls):
            if not service.process_one():
                break
        result = {"status": service.store.stats(service.project, service.workspace),
                  "model_calls": service.model.calls}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
