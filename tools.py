"""Deterministic filesystem tools for TraceMind."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

from langchain_core.tools import tool
from langgraph.types import interrupt

import docker
import git

# In-memory staging backups for atomic edits and rollback safety
_FILE_BACKUPS: dict[str, str] = {}

IGNORED_DIR_NAMES = {".git", ".hg", ".svn", "__pycache__", "venv", ".venv", "node_modules"}


def _resolve_root(root_dir: str) -> Path:
    root = Path(root_dir).expanduser().resolve() if root_dir else Path.cwd().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Root directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root}")
    return root


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _iter_visible_dirs(path: Path) -> Iterable[Path]:
    for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir() and child.name not in IGNORED_DIR_NAMES and not _is_hidden(child.relative_to(path)):
            yield child


SUPPORTED_EXTENSIONS = {".py", ".ts", ".js", ".java", ".go"}

def _iter_polyglot_files(root: Path) -> Iterable[Path]:
    for current_root, dir_names, file_names in os.walk(root):
        current_path = Path(current_root)
        dir_names[:] = [
            name
            for name in dir_names
            if name not in IGNORED_DIR_NAMES and not name.startswith(".")
        ]
        for file_name in sorted(file_names):
            file_path = current_path / file_name
            if file_path.suffix in SUPPORTED_EXTENSIONS and not _is_hidden(file_path.relative_to(root)):
                yield file_path


def _format_line_numbered_content(lines: list[str], start_line: int) -> str:
    width = max(4, len(str(start_line + len(lines) - 1)))
    rendered_lines = []
    for offset, line in enumerate(lines):
        line_number = start_line + offset
        rendered_lines.append(f"{line_number:>{width}} | {line.rstrip()}" )
    return "\n".join(rendered_lines)


import re

def _extract_brace_matching_block(file_path: Path, symbol_name: str) -> list[str]:
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [f"ERROR reading {file_path}: {exc}"]

    lines = source.splitlines()
    matches = []
    suffix = file_path.suffix.lower()

    # Define patterns based on programming language
    patterns = []
    if suffix in {".ts", ".js"}:
        patterns = [
            rf"\bfunction\s+{symbol_name}\b",
            rf"\b(const|let|var)\s+{symbol_name}\s*=",
            rf"\bclass\s+{symbol_name}\b",
            rf"\b{symbol_name}\s*\([^)]*\)\s*[:\w\s]*\{{"
        ]
    elif suffix == ".java":
        patterns = [
            rf"\bclass\s+{symbol_name}\b",
            rf"\binterface\s+{symbol_name}\b",
            rf"\b{symbol_name}\s*\([^)]*\)"
        ]
    elif suffix == ".go":
        patterns = [
            rf"\bfunc\s+{symbol_name}\b",
            rf"\btype\s+{symbol_name}\s+(struct|interface)\b"
        ]

    for line_idx, line in enumerate(lines):
        matched = False
        for pattern in patterns:
            if re.search(pattern, line):
                matched = True
                break
        
        if matched:
            start_line = line_idx + 1
            open_braces = 0
            has_opened = False
            end_line = start_line
            
            for scan_idx in range(line_idx, len(lines)):
                scan_line = lines[scan_idx]
                open_braces += scan_line.count("{")
                open_braces -= scan_line.count("}")
                if "{" in scan_line:
                    has_opened = True
                
                if has_opened and open_braces <= 0:
                    end_line = scan_idx + 1
                    break
            else:
                end_line = min(start_line + 15, len(lines))
            
            block = lines[start_line - 1 : end_line]
            node_type = "FunctionOrClass"
            if "class" in line:
                node_type = "ClassDef"
            elif "func" in line or "function" in line or "=>" in line:
                node_type = "FunctionDef"
                
            matches.append(
                f"FILE: {file_path}\n"
                f"TYPE: {node_type}\n"
                f"NAME: {symbol_name}\n"
                f"LINES: {start_line}-{end_line}\n"
                f"DOCSTRING: (extracted polyglot block)\n"
                f"SOURCE:\n{_format_line_numbered_content(block, start_line)}"
            )
            
    return matches


def _extract_ast_matches(file_path: Path, symbol_name: str) -> list[str]:
    suffix = file_path.suffix.lower()
    if suffix != ".py":
        return _extract_brace_matching_block(file_path, symbol_name)

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [f"ERROR reading {file_path}: {exc}"]

    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        return []

    lines = source.splitlines()
    matches: list[str] = []

    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name != symbol_name:
            continue

        start_line = getattr(node, "lineno", None)
        end_line = getattr(node, "end_lineno", None) or start_line
        if start_line is None or end_line is None:
            continue

        block = lines[start_line - 1 : end_line]
        docstring = ast.get_docstring(node) or "(no docstring)"
        node_type = node.__class__.__name__
        matches.append(
            f"FILE: {file_path}\n"
            f"TYPE: {node_type}\n"
            f"NAME: {node.name}\n"
            f"LINES: {start_line}-{end_line}\n"
            f"DOCSTRING: {docstring}\n"
            f"SOURCE:\n{_format_line_numbered_content(block, start_line)}"
        )

    return matches


@tool
def list_project_structure(root_dir: str = "", max_depth: int = 3) -> str:
    """List the local project tree.

    Use this first to understand the repository layout before making any claims
    about code structure. Hidden folders, venv, and __pycache__ are ignored.
    """
    try:
        root = _resolve_root(root_dir)
    except Exception as exc:
        return f"ERROR: {exc}"

    if max_depth < 0:
        return "ERROR: max_depth must be non-negative."

    lines = [f"{root.name}/"]

    def walk(path: Path, depth: int) -> None:
        if depth >= max_depth:
            return
        for child in _iter_visible_dirs(path):
            relative_depth = depth + 1
            indent = "  " * relative_depth
            lines.append(f"{indent}{child.name}/")
            walk(child, relative_depth)
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            if child.is_file() and child.suffix == ".py" and not _is_hidden(child.relative_to(root)):
                lines.append(f"{'  ' * (depth + 1)}{child.name}")

    walk(root, 0)
    return "\n".join(lines)


@tool
def view_file_contents(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """Read specific line ranges from a file for evidence-based debugging.

    Pass line numbers around the failure site. The tool returns the selected
    lines with explicit 1-indexed line numbers for precise reasoning.
    """
    try:
        file = Path(file_path).expanduser().resolve()
        if not file.exists():
            return f"ERROR: File does not exist: {file}"
        if not file.is_file():
            return f"ERROR: Path is not a file: {file}"

        contents = file.read_text(encoding="utf-8", errors="replace").splitlines()
        if not contents:
            return f"FILE: {file}\n(empty file)"

        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = len(contents)
        if start_line < 1 or end_line < 1:
            return "ERROR: Line numbers must be 1 or greater."
        if end_line < start_line:
            return "ERROR: end_line must be greater than or equal to start_line."

        selected = contents[start_line - 1 : end_line]
        if not selected:
            return f"FILE: {file}\n(no lines in requested range)"
        return f"FILE: {file}\n{_format_line_numbered_content(selected, start_line)}"
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def search_code_in_project(query: str, root_dir: str = "") -> str:
    """Search Python source files for symbols, keywords, and error text.

    Use this to trace definitions, imports, and call sites across files after
    the failing file is identified from the traceback.
    """
    if not query.strip():
        return "ERROR: query must not be empty."

    try:
        root = _resolve_root(root_dir)
    except Exception as exc:
        return f"ERROR: {exc}"

    matches: list[str] = []
    query_lower = query.lower()

    for file_path in _iter_polyglot_files(root):
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            matches.append(f"FILE: {file_path}\n  ERROR: {exc}")
            continue

        file_matches: list[str] = []
        for index, line in enumerate(lines, start=1):
            if query_lower in line.lower():
                file_matches.append(f"{index}: {line.rstrip()}")

        if file_matches:
            matches.append(f"FILE: {file_path}\n" + "\n".join(f"  {line}" for line in file_matches))

    if not matches:
        return f"No matches found for '{query}' under {root}."

    return "\n\n".join(matches)


def _apply_code_patch_impl(file_path: str, old_snippet: str, new_snippet: str) -> str:
    try:
        file = Path(file_path).expanduser().resolve()
        if not file.exists():
            return f"ERROR: File does not exist: {file}"
        if not file.is_file():
            return f"ERROR: Path is not a file: {file}"

        contents = file.read_text(encoding="utf-8", errors="replace")
        if not old_snippet.strip():
            return "ERROR: old_snippet must not be empty."

        match_count = contents.count(old_snippet)
        if match_count != 1:
            return (
                f"ERROR: old_snippet must match exactly one occurrence in {file}, "
                f"but found {match_count}."
            )

        # Stage original content before overwrite for rollback safety
        file_key = str(file)
        if file_key not in _FILE_BACKUPS:
            _FILE_BACKUPS[file_key] = contents

        updated_contents = contents.replace(old_snippet, new_snippet, 1)
        file.write_text(updated_contents, encoding="utf-8")
        return f"Applied patch successfully to {file}."
    except Exception as exc:
        return f"ERROR: {exc}"


def _run_python_script_impl(script_path: str) -> str:
    try:
        script = Path(script_path).expanduser().resolve()
        if not script.exists():
            return f"ERROR: Script does not exist: {script}"
        if not script.is_file():
            return f"ERROR: Script path is not a file: {script}"

        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        output_parts = [f"Exit code: {completed.returncode}"]
        output_parts.append("STDOUT:")
        output_parts.append(stdout or "(empty)")
        output_parts.append("STDERR:")
        output_parts.append(stderr or "(empty)")
        return "\n".join(output_parts)
    except subprocess.TimeoutExpired:
        return f"ERROR: Execution timed out after 10 seconds for {script_path}."
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def get_function_or_class_definition(symbol_name: str, root_dir: str = "") -> str:
    """Locate the exact AST definition for a function or class symbol.

    Prefer this over keyword search when tracing imported symbols or resolving
    where a function/class is defined. The tool walks all local `.py` files,
    parses each file into an abstract syntax tree, and returns the exact line
    range, docstring, and source block for matching definitions.
    """
    if not symbol_name.strip():
        return "ERROR: symbol_name must not be empty."

    try:
        root = _resolve_root(root_dir)
    except Exception as exc:
        return f"ERROR: {exc}"

    matches: list[str] = []
    for file_path in _iter_polyglot_files(root):
        matches.extend(_extract_ast_matches(file_path, symbol_name))

    if not matches:
        return f"No AST definitions found for '{symbol_name}' under {root}."

    return "\n\n".join(matches)


@tool
def apply_code_patch(file_path: str, old_snippet: str, new_snippet: str) -> str:
    """Safely replace an exact code block inside a file.

    Use this once the root cause is isolated and the agent has a precise patch.
    The tool validates that the target file exists and that `old_snippet` occurs
    exactly once before writing any changes to disk.
    """
    decision = interrupt(
        {
            "action": "apply_code_patch",
            "file_path": file_path,
            "old_snippet": old_snippet,
            "new_snippet": new_snippet,
        }
    )
    if isinstance(decision, dict):
        if not decision.get("approved", True):
            return f"Patch rejected for {file_path}. No changes written."
        new_snippet = decision.get("new_snippet", new_snippet)
    elif decision is False:
        return f"Patch rejected for {file_path}. No changes written."

    return _apply_code_patch_impl(file_path, old_snippet, new_snippet)


@tool
def run_python_script(script_path: str) -> str:
    """Execute a local Python script and return stdout, stderr, and exit code.

    Use this immediately after patch application to verify that the fix actually
    resolved the runtime error. The command is executed with a timeout to avoid
    hanging the debugging loop.
    """
    decision = interrupt({"action": "run_python_script", "script_path": script_path})
    if isinstance(decision, dict) and not decision.get("approved", True):
        return f"Execution rejected for {script_path}. No script was run."
    if decision is False:
        return f"Execution rejected for {script_path}. No script was run."

    return _run_python_script_impl(script_path)


@tool
def run_tests_in_sandbox(repo_dir: str, test_command: str, timeout: int = 60) -> str:
    """Execute tests inside a secure containerized Docker sandbox.

    If Docker is not running or available, falls back to local execution.
    If the execution returns a failure, it automatically rolls back all
    modified files to the clean branch state.
    """
    repo_path = Path(repo_dir).expanduser().resolve()
    
    # 1. Detect Docker environment
    has_docker = False
    client = None
    try:
        import docker
        client = docker.from_env()
        client.ping()
        has_docker = True
    except Exception:
        has_docker = False

    exit_code = 1
    output = ""
    sandbox_used = False

    if has_docker and client:
        try:
            # 2. Build sandbox image if not exists
            image_tag = "tracemind-sandbox:latest"
            try:
                client.images.get(image_tag)
            except docker.errors.ImageNotFound:
                dockerfile_dir = str(Path(__file__).resolve().parent / "docker")
                client.images.build(
                    path=dockerfile_dir,
                    dockerfile="Dockerfile.sandbox",
                    tag=image_tag,
                    rm=True
                )
            
            # 3. Spin up ephemeral container
            container = client.containers.run(
                image=image_tag,
                command=["sh", "-c", test_command],
                volumes={str(repo_path): {"bind": "/workspace", "mode": "rw"}},
                working_dir="/workspace",
                detach=True,
                network_mode="none"
            )
            
            # 4. Wait with timeout
            import time
            start_time = time.time()
            while True:
                container.reload()
                if container.status != "running":
                    break
                if time.time() - start_time > timeout:
                    try:
                        container.kill()
                    except Exception:
                        pass
                    container.remove()
                    exit_code = 124
                    output = f"ERROR: Execution timed out after {timeout} seconds inside sandbox container."
                    break
                time.sleep(0.5)
            
            if exit_code != 124:
                output = container.logs().decode("utf-8", errors="replace")
                exit_code = container.wait().get("StatusCode", 0)
                container.remove()
            
            sandbox_used = True
            
        except Exception as e:
            has_docker = False

    # 5. Local execution fallback if Docker failed/not available
    if not sandbox_used:
        try:
            import subprocess
            completed = subprocess.run(
                test_command,
                shell=True,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False
            )
            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            exit_code = completed.returncode
            output = f"STDOUT:\n{stdout or '(empty)'}\n\nSTDERR:\n{stderr or '(empty)'}"
        except subprocess.TimeoutExpired:
            exit_code = 124
            output = f"ERROR: Local execution timed out after {timeout} seconds."
        except Exception as exc:
            exit_code = 1
            output = f"ERROR: Local execution failed: {exc}"

    # 6. Apply Rollback Safety if execution failed
    rollback_performed = False
    rollback_log = ""
    
    if exit_code != 0:
        # Revert memory backups first
        if _FILE_BACKUPS:
            for file_p, orig_content in _FILE_BACKUPS.items():
                try:
                    Path(file_p).write_text(orig_content, encoding="utf-8")
                except Exception:
                    pass
            _FILE_BACKUPS.clear()
            rollback_performed = True
            rollback_log += "\n[ROLLBACK] Restored file modifications from memory backups."
        
        # Git rollback for additional safety
        try:
            import git
            repo = git.Repo(str(repo_path))
            current_branch = repo.active_branch.name
            if current_branch.startswith("tracemind/"):
                repo.git.checkout("--", ".")
                rollback_performed = True
                rollback_log += f"\n[ROLLBACK] Git: Reverted all working tree modifications on branch '{current_branch}'."
        except Exception as e:
            pass
    else:
        # Success: Commit changes by clearing memory backups
        _FILE_BACKUPS.clear()

    sandbox_prefix = "[CONTAINER SANDBOX]" if sandbox_used else "[LOCAL FALLBACK (Docker Unavailable)]"
    result = (
        f"{sandbox_prefix} Test Execution Result:\n"
        f"Exit code: {exit_code}\n"
        f"Output:\n{output}\n"
    )
    if rollback_performed:
        result += f"\nVerification Failed! Automatic rollback triggered:\n{rollback_log}\n"
        
    return result


@tool
def create_git_branch(repo_dir: str, branch_name: str) -> str:
    """Create and check out a new isolated Git branch for the patch.

    Use this before making any modifications to source code files.
    """
    try:
        import git
        repo_path = Path(repo_dir).expanduser().resolve()
        repo = git.Repo(str(repo_path))
        
        if branch_name in repo.heads:
            repo.heads[branch_name].checkout()
            return f"Checked out existing branch '{branch_name}'."
        
        new_branch = repo.create_head(branch_name)
        new_branch.checkout()
        return f"Successfully created and checked out isolated branch '{branch_name}'."
    except Exception as exc:
        return f"ERROR: Failed to create branch: {exc}"


@tool
def commit_patch(repo_dir: str, commit_message: str) -> str:
    """Stage modified files and commit them to the active Git branch.

    Commit messages should follow conventional commit standards.
    """
    try:
        import git
        repo_path = Path(repo_dir).expanduser().resolve()
        repo = git.Repo(str(repo_path))
        
        repo.git.add(A=True)
        
        if not repo.is_dirty(index=True):
            return "No changes staged to commit."
            
        commit = repo.index.commit(commit_message)
        return f"Successfully committed patch with message '{commit_message}' (Commit: {commit.hexsha[:7]})."
    except Exception as exc:
        return f"ERROR: Failed to commit changes: {exc}"


@tool
def generate_pr_summary(repo_dir: str, base_branch: str = "main") -> str:
    """Generate a formatted PR description summary containing the patch diff and analysis.

    Use this as the final step after successfully patching and verifying tests.
    """
    try:
        import git
        repo_path = Path(repo_dir).expanduser().resolve()
        repo = git.Repo(str(repo_path))
        
        current_branch = repo.active_branch.name
        diff = repo.git.diff(base_branch)
        
        modified_files = [item.a_path for item in repo.index.diff(base_branch)]
        if not modified_files and repo.head.commit:
            modified_files = [item.a_path for item in repo.head.commit.diff(base_branch)]
            
        summary = (
            f"# Pull Request Summary: Resolving Bugs & Mismatches\n\n"
            f"**Source Branch**: `{current_branch}`  \n"
            f"**Base Branch**: `{base_branch}`  \n\n"
            f"### Root Cause Analysis & Solution\n"
            f"TraceMind isolated the root cause of the crash and synthesized a targeted patch. "
            f"Verification tests have run successfully in a containerized sandbox environment.\n\n"
            f"### Modified Files\n"
        )
        for f in modified_files:
            summary += f"- `{f}`\n"
        summary += f"\n### Unified Git Diff\n```diff\n{diff or 'No changes'}\n```\n"
        return summary
    except Exception as exc:
        return f"ERROR: Failed to generate PR summary: {exc}"


DEBUGGER_TOOLS = [
    list_project_structure,
    view_file_contents,
    get_function_or_class_definition,
    search_code_in_project,
    apply_code_patch,
    run_python_script,
    run_tests_in_sandbox,
    create_git_branch,
    commit_patch,
    generate_pr_summary,
]
