from dataclasses import asdict

from cortexterm.mcp import create_mcp_backed_tools
from cortexterm.skills import discover_skills
from cortexterm.tooling import ToolRegistry
from cortexterm.tools.api_tester import api_tester_tool
from cortexterm.tools.ask_user import ask_user_tool
from cortexterm.tools.code_nav import find_symbols_tool, find_references_tool, get_ast_info_tool
from cortexterm.tools.code_review import code_review_tool
from cortexterm.tools.db_explorer import db_explorer_tool
from cortexterm.tools.diff_viewer import diff_viewer_tool
from cortexterm.tools.docker_helper import docker_helper_tool
from cortexterm.tools.edit_file import edit_file_tool
from cortexterm.tools.file_tree import file_tree_tool
from cortexterm.tools.git import git_tool
from cortexterm.tools.governance_audit_tool import governance_audit_tool
from cortexterm.tools.grep_files import grep_files_tool
from cortexterm.tools.list_files import list_files_tool
from cortexterm.tools.load_skill import create_load_skill_tool
from cortexterm.tools.modify_file import modify_file_tool
from cortexterm.tools.multi_edit import multi_edit_tool
from cortexterm.tools.notebook_edit import notebook_edit_tool
from cortexterm.tools.patch_file import patch_file_tool
from cortexterm.tools.read_file import read_file_tool
from cortexterm.tools.remember import remember_tool
from cortexterm.tools.run_command import run_command_tool
from cortexterm.tools.run_with_debug import run_with_debug_tool
from cortexterm.tools.test_runner import test_runner_tool
from cortexterm.tools.todo_write import todo_write_tool
from cortexterm.tools.web_fetch import web_fetch_tool
from cortexterm.tools.web_search import web_search_tool
from cortexterm.tools.write_file import write_file_tool


def create_default_tool_registry(cwd: str, runtime: dict | None = None) -> ToolRegistry:
    skills = [asdict(skill) for skill in discover_skills(cwd)]
    mcp = create_mcp_backed_tools(cwd=cwd, mcp_servers=dict(runtime.get("mcpServers", {})) if runtime else {})
    return ToolRegistry(
        [
            # User interaction
            ask_user_tool,
            remember_tool,
            # File operations
            list_files_tool,
            grep_files_tool,
            read_file_tool,
            write_file_tool,
            modify_file_tool,
            edit_file_tool,
            patch_file_tool,
            # Command execution
            run_command_tool,
            run_with_debug_tool,
            # Web tools
            web_fetch_tool,
            web_search_tool,
            api_tester_tool,
            # Task management
            todo_write_tool,
            # Git workflow
            git_tool,
            # Notebook editing
            notebook_edit_tool,
            # Code intelligence
            find_symbols_tool,
            find_references_tool,
            get_ast_info_tool,
            multi_edit_tool,
            code_review_tool,
            # Visualization
            file_tree_tool,
            diff_viewer_tool,
            # Testing & Debugging
            test_runner_tool,
            # Database & Docker (NEW!)
            db_explorer_tool,
            docker_helper_tool,
            # Governance audit
            governance_audit_tool,
            # Skills
            create_load_skill_tool(cwd),
            # MCP tools
            *mcp["tools"],
        ],
        skills=skills,
        mcp_servers=mcp["servers"],
        disposer=mcp["dispose"],
    )
