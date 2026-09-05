"""Quick test to verify CortexTerm TUI functionality in mock mode."""

import os
import sys
from pathlib import Path

# Set mock mode before importing
os.environ["CORTEXTERM_MODEL_MODE"] = "mock"

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from cortexterm.config import load_runtime_config
from cortexterm.permissions import PermissionManager
from cortexterm.prompt import build_system_prompt
from cortexterm.tools import create_default_tool_registry
from cortexterm.tty_app import run_tty_app

def main():
    cwd = str(Path.cwd())
    print("Starting CortexTerm in mock mode...")
    print()
    
    try:
        runtime = load_runtime_config(cwd)
    except Exception as e:
        print(f"⚠️  Config warning: {e}")
        runtime = None
    
    tools = create_default_tool_registry(cwd, runtime=runtime)
    permissions = PermissionManager(cwd, prompt=None)
    
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                cwd,
                permissions.get_summary(),
                {
                    "skills": tools.get_skills(),
                    "mcpServers": tools.get_mcp_servers(),
                },
            ),
        }
    ]
    
    print(f"✓ Model: {runtime.get('model', 'mock') if runtime else 'mock'}")
    print(f"✓ Tools: {len(tools.list())} available")
    print(f"✓ Skills: {len(tools.get_skills())} discovered")
    print(f"✓ MCP Servers: {len(tools.get_mcp_servers())} configured")
    print()
    print("Starting TUI... (type /exit to quit)")
    print()
    
    try:
        run_tty_app(
            runtime=runtime,
            tools=tools,
            model=None,  # Will use mock from env
            messages=messages,
            cwd=cwd,
            permissions=permissions,
        )
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        tools.dispose()

if __name__ == "__main__":
    main()
