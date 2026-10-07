"""LangGraph orchestration for TraceMind."""

from __future__ import annotations

import os
import uuid
import time
from functools import lru_cache
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command

from tools import (
    DEBUGGER_TOOLS,
    _apply_code_patch_impl,
    _run_python_script_impl,
    list_project_structure,
    view_file_contents,
    get_function_or_class_definition,
    search_code_in_project,
    apply_code_patch,
    run_tests_in_sandbox,
    create_git_branch,
    commit_patch,
    generate_pr_summary,
)

load_dotenv()

SYSTEM_PROMPT = """You are TraceMind, an autonomous cross-file polyglot debugging agent.

Follow this strict ReAct operational workflow:
1. Always start by calling list_project_structure to understand the repository layout.
2. Inspect failing code lines using view_file_contents with 10-15 lines above and below the crash.
3. Trace cross-file dependencies using search_code_in_project and get_function_or_class_definition.
4. Git Isolation: Before applying any code patches, create a new isolated git branch using create_git_branch (e.g. branch name: 'tracemind/fix-keyerror-101').
5. Apply Code Patch: Once the root cause is isolated, use apply_code_patch to apply the fix directly to the source file.
6. Sandbox Verification: Run tests using run_tests_in_sandbox to verify the patch. (Note: if tests fail, the modifications will be automatically rolled back, allowing you to try a clean alternative patch).
7. Commit Patch: Stage files and create a descriptive commit following conventional commit standards using commit_patch.
8. PR Summary: Generate a unified Git diff and PR description using generate_pr_summary.
9. Summarize the root cause, modifications, and testing results in your final response.

Rules:
- You must use the provided filesystem tools for every repository read, search, and write. Never use model memory, guessed content, or direct filesystem access as a substitute.
- Never guess or assume file contents without inspecting actual source code.
- Stay grounded in tool observations and do not invent file contents, imports, or runtime state.
- Select the verification command for the repository language (for example, `node app.js`, `npm test`, `pytest`, `go test`, or a Java build command), and run it through run_tests_in_sandbox.
"""

SUPPORTED_PROVIDERS = {"openai", "google", "groq", "github", "ollama"}
OFFLINE_DEMO_PROVIDER = "offline-demo"
PROJECT_ROOT = Path(__file__).resolve().parent
CHECKPOINTER = MemorySaver()


def _required_api_key_name(provider: str) -> str | None:
    provider = provider.lower()
    if provider == "openai":
        return "OPENAI_API_KEY"
    if provider == "google":
        return "GOOGLE_API_KEY"
    if provider == "groq":
        return "GROQ_API_KEY"
    if provider == "github":
        return "GITHUB_TOKEN"
    if provider == "ollama":
        return None
    return "OPENAI_API_KEY"


def _build_llm(provider: str | None = None, model_name: str | None = None):
    provider = (provider or os.getenv("TRACE_MIND_PROVIDER", "openai")).strip().lower()
    model_name = (model_name or os.getenv("TRACE_MIND_MODEL", "gpt-4o-mini")).strip()

    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")

    api_key_name = _required_api_key_name(provider)

    if api_key_name and not os.getenv(api_key_name):
        raise RuntimeError(f"Missing required environment variable: {api_key_name}")

    if provider == "google":
        return ChatOpenAI(
            model=model_name,
            api_key=os.environ.get("GOOGLE_API_KEY"),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            temperature=0,
            use_responses_api=False,
            request_timeout=60,
            max_retries=0,
        )

    base_url_map = {
        "openai": os.getenv("TRACE_MIND_BASE_URL", ""),
        "groq": os.getenv("TRACE_MIND_BASE_URL", "https://api.groq.com/openai/v1"),
        "github": os.getenv("TRACE_MIND_BASE_URL", "https://models.inference.ai.azure.com"),
        "ollama": os.getenv("TRACE_MIND_BASE_URL", "http://localhost:11434/v1"),
    }
    base_url = base_url_map[provider] or None
    api_key_map = {
        "openai": os.getenv("OPENAI_API_KEY"),
        "groq": os.getenv("GROQ_API_KEY"),
        "github": os.getenv("GITHUB_TOKEN"),
        "ollama": os.getenv("OLLAMA_API_KEY", "ollama"),
    }

    return ChatOpenAI(
        model=model_name,
        temperature=0,
        api_key=api_key_map[provider],
        base_url=base_url,
        request_timeout=60,
        max_retries=0,
    )


def _trim_state_for_model(state: dict[str, Any]) -> dict[str, Any]:
    messages = list(state.get("messages", []))
    if len(messages) <= 2:
        return state

    first_message = messages[0]
    if not isinstance(first_message, HumanMessage):
        first_message = messages[0]

    trimmed_tail = trim_messages(
        messages[1:],
        max_tokens=3000,
        strategy="last",
        token_counter="approximate",
        include_system=False,
    )
    return {**state, "messages": [first_message, *trimmed_tail]}


def _build_pending_action(tool_call: dict[str, Any]) -> dict[str, Any]:
    args = tool_call.get("args") or {}
    file_path = args.get("file_path") or args.get("script_path") or args.get("root_dir") or ""
    return {
        "tool_name": tool_call.get("name", "unknown"),
        "tool_call_id": tool_call.get("id", ""),
        "args": args,
        "file_path": file_path,
        "old_snippet": args.get("old_snippet", ""),
        "new_snippet": args.get("new_snippet", ""),
        "raw_tool_call": tool_call,
    }


def _run_offline_demo_session(
    stack_trace_input: str,
    command: Command | None = None,
    pending_action_override: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    project_root = str(PROJECT_ROOT)
    calculator_path = str(PROJECT_ROOT / "sample_bug" / "utils" / "calculator.py")
    main_path = str(PROJECT_ROOT / "sample_bug" / "main.py")
    yield {
        "step": 0,
        "event": "session_started",
        "provider": "offline-demo",
        "model": "rule-based",
        "thread_id": str(uuid.uuid4()),
    }
    messages: list[BaseMessage] = [HumanMessage(content=stack_trace_input)]
    structure_output = DEBUGGER_TOOLS[0].invoke({"root_dir": project_root, "max_depth": 3})
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_project_structure",
                        "args": {"root_dir": project_root, "max_depth": 3},
                        "id": "demo-call-1",
                    }
                ],
            ),
            ToolMessage(content=structure_output, tool_call_id="demo-call-1", name="list_project_structure"),
        ]
    )
    yield {
        "step": 1,
        "event": "state",
        "message_count": len(messages),
        "messages": [_summarize_message(message) for message in messages],
    }

    definition_output = DEBUGGER_TOOLS[2].invoke(
        {"symbol_name": "calculate_discounted_price", "root_dir": project_root}
    )
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_function_or_class_definition",
                        "args": {"symbol_name": "calculate_discounted_price", "root_dir": project_root},
                        "id": "demo-call-2",
                    }
                ],
            ),
            ToolMessage(
                content=definition_output,
                tool_call_id="demo-call-2",
                name="get_function_or_class_definition",
            ),
        ]
    )
    yield {
        "step": 2,
        "event": "state",
        "message_count": len(messages),
        "messages": [_summarize_message(message) for message in messages],
    }

    search_output = DEBUGGER_TOOLS[3].invoke({"query": "price", "root_dir": project_root})
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_code_in_project",
                        "args": {"query": "price", "root_dir": project_root},
                        "id": "demo-call-3",
                    }
                ],
            ),
            ToolMessage(content=search_output, tool_call_id="demo-call-3", name="search_code_in_project"),
        ]
    )
    yield {
        "step": 3,
        "event": "state",
        "message_count": len(messages),
        "messages": [_summarize_message(message) for message in messages],
    }

    view_output = DEBUGGER_TOOLS[1].invoke(
        {"file_path": calculator_path, "start_line": 1, "end_line": 20}
    )
    pending_action = pending_action_override or {
        "tool_name": "apply_code_patch",
        "tool_call_id": "demo-call-4",
        "args": {
            "file_path": calculator_path,
            "old_snippet": '    return product_data["price"] * 0.9',
            "new_snippet": '    return product_data["price_usd"] * 0.9',
        },
        "file_path": calculator_path,
        "old_snippet": '    return product_data["price"] * 0.9',
        "new_snippet": '    return product_data["price_usd"] * 0.9',
    }
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "view_file_contents",
                        "args": {
                            "file_path": calculator_path,
                            "start_line": 1,
                            "end_line": 20,
                        },
                        "id": "demo-call-4",
                    }
                ],
            ),
            ToolMessage(content=view_output, tool_call_id="demo-call-4", name="view_file_contents"),
        ]
    )
    yield {
        "step": 4,
        "event": "state",
        "message_count": len(messages),
        "messages": [_summarize_message(message) for message in messages],
    }
    if command is None:
        yield {
            "step": 4,
            "event": "interrupted",
            "provider": "offline-demo",
            "model": "rule-based",
            "thread_id": str(uuid.uuid4()),
            "pending_action": pending_action,
            "messages": [_summarize_message(message) for message in messages],
        }
        return

    feedback_message = None
    if isinstance(command, Command) and command.update:
        feedback_message = "User provided manual patch feedback before continuing."
        if isinstance(command.update, dict):
            feedback_payload = command.update.get("messages")
            if feedback_payload:
                last_feedback = feedback_payload[-1]
                feedback_message = getattr(last_feedback, "content", str(last_feedback))

    if isinstance(command, Command) and command.resume is not None:
        if pending_action_override is not None:
            pending_action = pending_action_override
        patch_output = _apply_code_patch_impl(
            pending_action["file_path"],
            pending_action["old_snippet"],
            pending_action["new_snippet"],
        )
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "apply_code_patch",
                            "args": pending_action["args"],
                            "id": pending_action["tool_call_id"],
                        }
                    ],
                ),
                ToolMessage(
                    content=patch_output,
                    tool_call_id=pending_action["tool_call_id"],
                    name="apply_code_patch",
                ),
            ]
        )
        yield {
            "step": 5,
            "event": "state",
            "message_count": len(messages),
            "messages": [_summarize_message(message) for message in messages],
        }

        run_output = _run_python_script_impl(main_path)
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_python_script",
                            "args": {"script_path": main_path},
                            "id": "demo-call-5",
                        }
                    ],
                ),
                ToolMessage(content=run_output, tool_call_id="demo-call-5", name="run_python_script"),
                AIMessage(
                    content=(
                        "Root cause: sample_bug/utils/calculator.py read the wrong dictionary key. "
                        "I patched it to use price_usd and verified the script exits cleanly.\n\n"
                        "Patch applied: calculator.py now returns product_data[\"price_usd\"] * 0.9.\n"
                        "Verification: sample_bug/main.py prints `Final Price: $44.99`."
                    )
                ),
            ]
        )
        yield {
            "step": 6,
            "event": "state",
            "message_count": len(messages),
            "messages": [_summarize_message(message) for message in messages],
        }
        return

    if feedback_message:
        yield {
            "step": 5,
            "event": "rejected",
            "feedback": feedback_message,
            "messages": [_summarize_message(message) for message in messages],
        }


@lru_cache(maxsize=8)
def _build_tracemind_agent(provider: str, model_name: str):
    llm = _build_llm(provider=provider, model_name=model_name)

    # 1. Triage Agent Node
    def triage_node(state):
        triage_prompt = (
            "You are the Triage & Strategy Agent of TraceMind.\n"
            "Analyze the stack trace input, locate the failing file, and write a strategy plan for the Code Explorer sub-agent.\n"
            "Response must outline the suspected files to inspect and the plan."
        )
        messages = [SystemMessage(content=SYSTEM_PROMPT + "\n\n" + triage_prompt)] + state["messages"]
        response = llm.invoke(messages)
        response.content = f"### [Triage Agent Strategy Plan]\n{response.content}"
        return {"messages": [response]}

    # 2. Explorer Agent Sub-Graph Node
    explorer_prompt = (
        "You are the Code Explorer Agent of TraceMind.\n"
        "Your role is to list project files, search code, and view file contents to pinpoint the root cause.\n"
        "Use the tools provided (list_project_structure, view_file_contents, get_function_or_class_definition, search_code_in_project).\n"
        "Provide a detailed finding summary once you locate the bug."
    )
    explorer_agent = create_react_agent(
        model=llm,
        tools=[list_project_structure, view_file_contents, get_function_or_class_definition, search_code_in_project],
        prompt=SystemMessage(content=SYSTEM_PROMPT + "\n\n" + explorer_prompt)
    )
    def explorer_node(state):
        result = explorer_agent.invoke(state)
        if result["messages"] and isinstance(result["messages"][-1], AIMessage):
            result["messages"][-1].content = f"### [Code Explorer Agent Findings]\n{result['messages'][-1].content}"
        return {"messages": result["messages"]}

    # 3. Patcher Agent Sub-Graph Node
    patcher_prompt = (
        "You are the Patch & Verification Agent of TraceMind.\n"
        "Your role is to checkout a git branch, apply code patches, execute test suites, commit changes, and write the PR description.\n"
        "Use the tools provided (create_git_branch, apply_code_patch, run_tests_in_sandbox, commit_patch, generate_pr_summary).\n"
        "Remember that you must ask for approval via apply_code_patch. If tests fail, rollback is automated."
    )
    patcher_agent = create_react_agent(
        model=llm,
        tools=[create_git_branch, apply_code_patch, run_tests_in_sandbox, commit_patch, generate_pr_summary],
        prompt=SystemMessage(content=SYSTEM_PROMPT + "\n\n" + patcher_prompt)
    )
    def patcher_node(state):
        result = patcher_agent.invoke(state)
        if result["messages"] and isinstance(result["messages"][-1], AIMessage):
            result["messages"][-1].content = f"### [Patch & Verification Agent Resolution]\n{result['messages'][-1].content}"
        return {"messages": result["messages"]}

    #  supervisor routing decision
    def supervisor_edge(state) -> str:
        prompt = (
            "You are the Multi-Agent Supervisor Router for TraceMind.\n"
            "Given the conversation history, choose the next node to act:\n"
            "- 'explorer': if we need to inspect code, view definitions, or list files.\n"
            "- 'patcher': if we have findings and need to apply a fix, run sandbox tests, commit, or create a PR.\n"
            "- 'finish': if a PR diff has been successfully generated and tests passed.\n"
            "Response must be exactly one word: 'explorer', 'patcher', or 'finish'."
        )
        messages = state["messages"] + [SystemMessage(content=prompt)]
        try:
            response = llm.invoke(messages)
            decision = response.content.strip().lower()
            if "explorer" in decision:
                return "explorer"
            if "patcher" in decision:
                return "patcher"
            return "finish"
        except Exception:
            return "explorer"

    # Build the StateGraph
    workflow = StateGraph(MessagesState)
    workflow.add_node("triage", triage_node)
    workflow.add_node("explorer", explorer_node)
    workflow.add_node("patcher", patcher_node)
    
    workflow.add_edge(START, "triage")
    workflow.add_conditional_edges(
        "triage",
        supervisor_edge,
        {"explorer": "explorer", "patcher": "patcher", "finish": END}
    )
    workflow.add_conditional_edges(
        "explorer",
        supervisor_edge,
        {"explorer": "explorer", "patcher": "patcher", "finish": END}
    )
    workflow.add_conditional_edges(
        "patcher",
        supervisor_edge,
        {"explorer": "explorer", "patcher": "patcher", "finish": END}
    )
    
    return workflow.compile(checkpointer=CHECKPOINTER)


def _summarize_message(message: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": message.__class__.__name__}
    if isinstance(message, HumanMessage | AIMessage | SystemMessage):
        summary["content"] = message.content
    elif isinstance(message, ToolMessage):
        summary["content"] = message.content
        summary["tool_call_id"] = message.tool_call_id
        summary["name"] = getattr(message, "name", None)
    else:
        summary["content"] = getattr(message, "content", str(message))

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        summary["tool_calls"] = tool_calls
    return summary


def _detect_active_agent(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    name = message.get("name", "")
    tool_calls = message.get("tool_calls") or []
    
    read_tools = {"list_project_structure", "view_file_contents", "get_function_or_class_definition", "search_code_in_project"}
    write_tools = {"create_git_branch", "apply_code_patch", "run_tests_in_sandbox", "commit_patch", "generate_pr_summary"}
    
    if "[Triage Agent" in content:
        return "Triage Agent"
    if "[Code Explorer" in content:
        return "Code Explorer"
    if "[Patch & Verification" in content:
        return "Patch & Verification"
        
    for call in tool_calls:
        tool_name = call.get("name", "")
        if tool_name in read_tools:
            return "Code Explorer"
        if tool_name in write_tools:
            return "Patch & Verification"
            
    if name in read_tools:
        return "Code Explorer"
    if name in write_tools:
        return "Patch & Verification"
        
    return "Supervisor"


def run_debugging_session(
    stack_trace_input: str,
    provider: str | None = None,
    model_name: str | None = None,
    thread_id: str | None = None,
    command: Command | None = None,
    pending_action_override: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield debugging events for the UI.

    The generator streams LangGraph state snapshots so the UI can render the
    agent's reasoning, filesystem observations, and final resolution.
    """
    provider = (provider or os.getenv("TRACE_MIND_PROVIDER", "openai")).strip().lower()
    model_name = (model_name or os.getenv("TRACE_MIND_MODEL", "gpt-4o-mini")).strip()
    thread_id = thread_id or str(uuid.uuid4())
    if provider == OFFLINE_DEMO_PROVIDER:
        yield from _run_offline_demo_session(
            stack_trace_input,
            command=command,
            pending_action_override=pending_action_override,
        )
        return

    start_time = time.time()
    try:
        tracemind_agent = _build_tracemind_agent(provider, model_name)
    except Exception as exc:
        yield {
            "step": 0,
            "event": "error",
            "provider": provider,
            "model": model_name,
            "thread_id": thread_id,
            "message": f"Unable to start autonomous analysis: {type(exc).__name__}: {exc}",
            "messages": [],
            "telemetry": {
                "elapsed_time": 0.0,
                "active_agent": "Error",
                "step_count": 0,
                "estimated_tokens": 0,
            },
        }
        return
    
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 30,
    }
    input_payload: Any = command if command is not None else {"messages": [HumanMessage(content=stack_trace_input)]}
    yield {
        "step": 0,
        "event": "session_started",
        "provider": provider,
        "model": model_name,
        "thread_id": thread_id,
        "telemetry": {
            "elapsed_time": 0.0,
            "active_agent": "Triage Agent",
            "step_count": 0,
            "estimated_tokens": 800
        }
    }

    latest_messages: list[BaseMessage] = []
    interrupted = False
    try:
        for step_index, snapshot in enumerate(
            tracemind_agent.stream(input_payload, config=config, stream_mode="values"),
            start=1,
        ):
            if isinstance(snapshot, dict) and "__interrupt__" in snapshot:
                interrupted = True
                break

            messages = snapshot.get("messages", []) if isinstance(snapshot, dict) else []
            if messages:
                latest_messages = list(messages)

            elapsed_time = round(time.time() - start_time, 2)
            active_agent = "Supervisor"
            messages_summarized = [_summarize_message(message) for message in messages]
            if messages_summarized:
                active_agent = _detect_active_agent(messages_summarized[-1])

            yield {
                "step": step_index,
                "event": "state",
                "message_count": len(messages),
                "messages": messages_summarized,
                "telemetry": {
                    "elapsed_time": elapsed_time,
                    "active_agent": active_agent,
                    "step_count": step_index,
                    "estimated_tokens": step_index * 1250 + 800
                }
            }
    except Exception as exc:
        elapsed_time = round(time.time() - start_time, 2)
        yield {
            "step": max(1, len(latest_messages)),
            "event": "error",
            "provider": provider,
            "model": model_name,
            "thread_id": thread_id,
            "message": (
                f"Autonomous analysis stopped after {elapsed_time}s: "
                f"{type(exc).__name__}: {exc}"
            ),
            "messages": [_summarize_message(message) for message in latest_messages],
            "telemetry": {
                "elapsed_time": elapsed_time,
                "active_agent": "Error",
                "step_count": max(1, len(latest_messages)),
                "estimated_tokens": max(1, len(latest_messages)) * 1250 + 800
            }
        }
        return

    if interrupted:
        pending_tool_call: dict[str, Any] | None = None
        for message in reversed(latest_messages):
            if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
                pending_tool_call = _build_pending_action(message.tool_calls[0])
                break

        elapsed_time = round(time.time() - start_time, 2)
        yield {
            "step": max(1, len(latest_messages)),
            "event": "interrupted",
            "provider": provider,
            "model": model_name,
            "thread_id": thread_id,
            "pending_action": pending_tool_call,
            "messages": [_summarize_message(message) for message in latest_messages],
            "telemetry": {
                "elapsed_time": elapsed_time,
                "active_agent": "Patch & Verification",
                "step_count": max(1, len(latest_messages)),
                "estimated_tokens": max(1, len(latest_messages)) * 1250 + 800
            }
        }
