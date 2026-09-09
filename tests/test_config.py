import json

import cortexterm.config as config
from cortexterm.config import get_env, merge_settings


def test_merge_settings_merges_env_and_mcp_servers() -> None:
    merged = merge_settings(
        {
            "env": {"A": "1"},
            "mcpServers": {
                "fs": {"command": "npx", "args": ["a"], "env": {"X": "1"}}
            },
        },
        {
            "env": {"B": "2"},
            "mcpServers": {
                "fs": {"command": "uvx", "env": {"Y": "2"}},
                "search": {"command": "python"},
            },
        },
    )

    assert merged["env"] == {"A": "1", "B": "2"}
    assert merged["mcpServers"]["fs"]["command"] == "uvx"
    assert merged["mcpServers"]["fs"]["args"] == ["a"]
    assert merged["mcpServers"]["fs"]["env"] == {"X": "1", "Y": "2"}
    assert merged["mcpServers"]["search"]["command"] == "python"


def test_reads_cortexterm_environment_variable(monkeypatch) -> None:
    monkeypatch.delenv("CORTEXTERM_MODEL", raising=False)
    assert get_env("CORTEXTERM_MODEL") is None

    monkeypatch.setenv("CORTEXTERM_MODEL", "configured-model")
    assert get_env("CORTEXTERM_MODEL") == "configured-model"


def test_cortexterm_settings_override_global_sources(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    global_mcp = tmp_path / "mcp.json"
    claude_settings = tmp_path / "claude-settings.json"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    claude_settings.write_text(
        json.dumps({"model": "claude-default", "env": {"SHARED": "claude"}}),
        encoding="utf-8",
    )
    global_mcp.write_text(
        json.dumps({"mcpServers": {"files": {"command": "global"}}}),
        encoding="utf-8",
    )
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"files": {"command": "project"}}}),
        encoding="utf-8",
    )
    settings.write_text(
        json.dumps({"model": "configured-model", "env": {"SHARED": "configured"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "CORTEXTERM_SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "CORTEXTERM_MCP_PATH", global_mcp)
    monkeypatch.setattr(config, "CLAUDE_SETTINGS_PATH", claude_settings)

    effective = config.load_effective_settings(project_dir)

    assert effective["model"] == "configured-model"
    assert effective["env"]["SHARED"] == "configured"
    assert effective["mcpServers"]["files"]["command"] == "project"


def test_runtime_config_loads_pi_compaction_settings(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "claude-sonnet-4-20250514",
                "compaction": {
                    "strategy": "legacy",
                    "reserveTokens": 8000,
                    "keepRecentTokens": 12000,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CORTEXTERM_SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "CORTEXTERM_MCP_PATH", tmp_path / "mcp.json")
    monkeypatch.setattr(config, "CLAUDE_SETTINGS_PATH", tmp_path / "claude.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    runtime = config.load_runtime_config(tmp_path)

    assert runtime["compaction"] == {
        "enabled": True,
        "strategy": "legacy",
        "reserveTokens": 8000,
        "keepRecentTokens": 12000,
    }

