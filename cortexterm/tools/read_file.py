from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path

from cortexterm.tooling import ToolDefinition, ToolResult
from cortexterm.workspace import resolve_tool_path

DEFAULT_READ_LIMIT = 8000
MAX_READ_LIMIT = 20000
MAX_READ_LINES = 200

# 文件内容缓存，避免重复读取同一文件
# 缓存键：(文件路径，修改时间) -> (内容, 缓存时间)
_file_cache: dict[tuple[str, float], tuple[str, float]] = {}
_FILE_CACHE_TTL = 2.0  # 缓存有效期 2 秒


def _get_cached_file_content(target: Path) -> str:
    """获取文件内容，使用缓存避免重复读取"""
    try:
        stat = target.stat()
        mtime = stat.st_mtime
        cache_key = (str(target), mtime)
        
        if cache_key in _file_cache:
            content, cache_time = _file_cache[cache_key]
            # 检查是否过期
            now = time.monotonic()
            if now - cache_time <= _FILE_CACHE_TTL:
                return content
        
        # 清理过期缓存
        now = time.monotonic()
        expired_keys = [k for k, (c, t) in _file_cache.items() if now - t > _FILE_CACHE_TTL]
        for k in expired_keys:
            del _file_cache[k]
        
        # 读取并缓存
        content = target.read_text(encoding="utf-8")
        _file_cache[cache_key] = (content, time.monotonic())
        return content
    except OSError:
        # 如果文件不存在或无法访问，直接读取
        return target.read_text(encoding="utf-8")


def _validate(input_data: dict) -> dict:
    path = input_data.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    if "start_line" in input_data or "end_line" in input_data:
        if "offset" in input_data or "limit" in input_data:
            raise ValueError("Use start_line/end_line OR offset/limit, not both")
        start = input_data.get("start_line", 1)
        end = input_data.get("end_line", start + MAX_READ_LINES - 1) if isinstance(start, int) else None
        if isinstance(start, bool) or not isinstance(start, int) or start < 1:
            raise ValueError("start_line must be an integer >= 1")
        if isinstance(end, bool) or not isinstance(end, int) or end < start:
            raise ValueError("end_line must be an integer >= start_line")
        return {"path": path, "start_line": start, "end_line": end}
    offset = int(input_data.get("offset", 0))
    limit = int(input_data.get("limit", DEFAULT_READ_LIMIT))
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 1 or limit > MAX_READ_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READ_LIMIT}")
    return {"path": path, "offset": offset, "limit": limit}


def _read_lines(content: str, input_data: dict) -> ToolResult:
    lines = content.splitlines(keepends=True)
    start = input_data["start_line"]
    requested_end = input_data.get("end_line", start + MAX_READ_LINES - 1)
    end = min(requested_end, start + MAX_READ_LINES - 1, len(lines))
    if start > len(lines):
        return ToolResult(ok=True, output=f"FILE: {input_data['path']}\nTOTAL_LINES: {len(lines)}\nNo lines at or after {start}.")
    offset = sum(map(len, lines[:start - 1]))
    remaining = MAX_READ_LIMIT
    output, consumed = [], 0
    partial = False
    last_line = start - 1
    for number in range(start, end + 1):
        line = lines[number - 1]
        part = line[:remaining]
        output.append(f"{number}: {part.rstrip(chr(10)).rstrip(chr(13))}")
        consumed += len(part)
        remaining -= len(part)
        last_line = number
        partial = len(part) < len(line)
        if not remaining:
            break
    if partial:
        continuation = f"yes - line {last_line} is partial; continue with offset {offset + consumed} (character mode)"
    elif last_line < min(requested_end, len(lines)):
        continuation = f"yes - continue with start_line {last_line + 1}"
    else:
        continuation = "no"
    header = [f"FILE: {input_data['path']}", f"LINES: {start}-{last_line}",
              f"TOTAL_LINES: {len(lines)}", f"OFFSET: {offset}", f"END: {offset + consumed}",
              f"TRUNCATED: {continuation}", ""]
    return ToolResult(ok=True, output="\n".join(header + output))


def _run(input_data: dict, context) -> ToolResult:
    target = resolve_tool_path(context, input_data["path"], "read")

    try:
        # 使用缓存读取
        content = _get_cached_file_content(target)
    except UnicodeDecodeError:
        return ToolResult(
            ok=False,
            output=f"File {input_data['path']} appears to be binary. Cannot read as text.",
        )
    
    if "start_line" in input_data:
        return _read_lines(content, input_data)
    offset = input_data["offset"]
    limit = input_data["limit"]
    end = min(len(content), offset + limit)
    chunk = content[offset:end]
    truncated = end < len(content)
    header = "\n".join(
        [
            f"FILE: {input_data['path']}",
            f"OFFSET: {offset}",
            f"END: {end}",
            f"TOTAL_CHARS: {len(content)}",
            f"TRUNCATED: {'yes - call read_file again with offset ' + str(end) if truncated else 'no'}",
            "",
        ]
    )
    return ToolResult(ok=True, output=header + chunk)


read_file_tool = ToolDefinition(
    name="read_file",
    description=("Read a UTF-8 file. After search gives line numbers, use start_line/end_line "
                 "(1-based, inclusive; up to 200 lines and 20000 source characters per call). "
                 "Alternatively use offset/limit for 0-based character paging. Do not mix the two modes."),
    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"},
                  "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"]},
    validator=_validate,
    run=_run,
)

