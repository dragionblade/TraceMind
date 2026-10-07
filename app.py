"""Streamlit UI for TraceMind."""

from __future__ import annotations

import os
from pathlib import Path
import difflib
import uuid
from typing import Any

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent import run_debugging_session

PROJECT_ROOT = Path(__file__).resolve().parent


def _clone_if_git_url(repo_dir_or_url: str) -> str:
    if repo_dir_or_url.startswith(("http://", "https://", "git@")):
        try:
            import tempfile
            import git
            clone_dir = os.path.join(tempfile.gettempdir(), "tracemind_cloned_" + str(uuid.uuid4())[:8])
            os.makedirs(clone_dir, exist_ok=True)
            git.Repo.clone_from(repo_dir_or_url, clone_dir)
            return clone_dir
        except Exception as e:
            st.error(f"Failed to clone repository: {e}")
            return repo_dir_or_url
    return repo_dir_or_url


SAMPLE_TRACEBACK = """Traceback (most recent call last):
  File \"sample_bug/main.py\", line 6, in <module>
    print(calculate_discounted_price(product))
  File \"sample_bug/utils/calculator.py\", line 2, in calculate_discounted_price
    return product_data[\"price\"] * 0.9
KeyError: 'price'
"""

PROVIDER_OPTIONS = [
    {
        "label": "OpenAI - gpt-4o-mini",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "key_name": "OPENAI_API_KEY",
    },
    {
        "label": "Google Gemini - gemini-2.5-flash",
        "provider": "google",
        "model": "gemini-2.5-flash",
        "key_name": "GOOGLE_API_KEY",
    },
    {
        "label": "Groq - llama-3.3-70b-versatile",
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "key_name": "GROQ_API_KEY",
    },
    {
        "label": "Local Ollama - qwen2.5-coder:7b",
        "provider": "ollama",
        "model": "qwen2.5-coder:7b",
        "key_name": "OLLAMA_API_KEY",
    },
]

READ_TOOL_NAMES = {
    "list_project_structure",
    "view_file_contents",
    "search_code_in_project",
}

WRITE_TOOL_NAMES = {
    "apply_code_patch",
    "run_python_script",
}


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or any(character in text for character in [":", "#", "\n", "\""]):
        return f'"{text.replace("\"", "\\\"")}"'
    return text


def _format_yaml(value: Any, indent: int = 0) -> str:
    padding = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{padding}{key}:")
                lines.append(_format_yaml(item, indent + 1))
            else:
                lines.append(f"{padding}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{padding}-")
                lines.append(_format_yaml(item, indent + 1))
            else:
                lines.append(f"{padding}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{padding}{_yaml_scalar(value)}"


def _tool_category(tool_name: str | None) -> tuple[str, str]:
    if tool_name in READ_TOOL_NAMES:
        return "Read Action", "read-action"
    if tool_name in WRITE_TOOL_NAMES:
        return "Write/Execute Action", "write-action"
    return "Action", "neutral-action"


def _badge_html(label: str, css_class: str) -> str:
    return f"<span class='action-badge {css_class}'>{label}</span>"


def _render_diff(file_path: str, old_snippet: str, new_snippet: str) -> None:
    diff_text = "\n".join(
        difflib.unified_diff(
            old_snippet.splitlines(),
            new_snippet.splitlines(),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
    )
    st.code(diff_text or "No diff", language="diff")


def _render_session_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event") == "session_started":
        st.info(f"Provider: {event.get('provider')} | Model: {event.get('model')} | Thread: {event.get('thread_id')}")
        return None

    if event.get("event") == "rejected":
        st.warning(event.get("feedback", "Patch rejected."))
        return None

    if event.get("event") == "state":
        messages = event.get("messages", [])
        rendered_messages = messages[-4:] if len(messages) > 4 else messages
        for message in rendered_messages:
            _render_message(message)
        return None

    if event.get("event") == "interrupted":
        messages = event.get("messages", [])
        for message in messages[-3:]:
            _render_message(message)
        return event.get("pending_action")

    if event.get("event") == "done":
        st.success(event.get("message", "TraceMind completed."))
        return None

    if event.get("event") == "error":
        st.error(event.get("message", "Autonomous analysis failed."))
    return None


def _run_session(
    stack_trace: str,
    provider: str,
    model_name: str,
    thread_id: str,
    command: Command | None = None,
    pending_action_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    pending_action: dict[str, Any] | None = None
    with st.status("Running autonomous debugging workflow", expanded=True) as status_box:
        for event in run_debugging_session(
            stack_trace,
            provider=provider,
            model_name=model_name,
            thread_id=thread_id,
            command=command,
            pending_action_override=pending_action_override,
        ):
            # Capture streamed telemetry metrics
            telemetry = event.get("telemetry")
            if telemetry:
                st.session_state.telemetry_history.append(telemetry)
                
            pending = _render_session_event(event)
            if event.get("event") == "session_started":
                status_box.write(f"Thread: {event.get('thread_id')}")
            elif event.get("event") == "state":
                status_box.write(f"Step {event.get('step')}")
            elif event.get("event") == "interrupted":
                status_box.warning("Execution paused for approval.")
                pending_action = pending
                break
            elif event.get("event") == "rejected":
                status_box.write("Patch rejected; agent should re-plan.")
            elif event.get("event") == "done":
                status_box.write(event.get("message", "TraceMind completed."))
                pending_action = None
            elif event.get("event") == "error":
                status_box.update(
                    label="Autonomous debugging stopped",
                    state="error",
                    expanded=True,
                )
                pending_action = None
    return pending_action


def _resume_pending_action(
    pending_action: dict[str, Any],
    edited_new_snippet: str,
    decision: str,
) -> Command:
    if decision == "reject":
        return Command(
            resume={
                "approved": False,
                "feedback": "User rejected this patch. Please analyze alternative root causes.",
            }
        )

    if decision == "edit":
        return Command(
            resume={
                "approved": True,
                "new_snippet": edited_new_snippet,
            }
        )

    if edited_new_snippet != pending_action.get("new_snippet", ""):
        return Command(
            resume={
                "approved": True,
                "new_snippet": edited_new_snippet,
            }
        )

    return Command(resume=True)


def _render_approval_card(pending_action: dict[str, Any], provider: str, model_name: str, thread_id: str, stack_trace: str) -> None:
    tool_name = pending_action.get("tool_name", "unknown")
    file_path = pending_action.get("file_path", "")
    old_snippet = pending_action.get("old_snippet", "")
    new_snippet_key = f"pending_new_snippet_{thread_id}_{pending_action.get('tool_call_id', 'call')}"
    approve_key = f"approve_{thread_id}_{pending_action.get('tool_call_id', 'call')}"
    edit_key = f"edit_{thread_id}_{pending_action.get('tool_call_id', 'call')}"
    reject_key = f"reject_{thread_id}_{pending_action.get('tool_call_id', 'call')}"
    current_new_snippet = st.session_state.get(new_snippet_key, pending_action.get("new_snippet", ""))

    with st.container(border=True):
        st.subheader("Action Approval Card")
        st.caption(f"Pending tool: {tool_name}")
        st.caption(f"Target: {file_path}")
        if old_snippet or current_new_snippet:
            _render_diff(file_path, old_snippet, current_new_snippet)
        edited_new_snippet = st.text_area(
            "Edit proposed patch",
            value=current_new_snippet,
            key=new_snippet_key,
            height=120,
        )

        approve_clicked = st.button("Approve & Resume", type="primary", width="stretch", key=approve_key)
        edit_clicked = st.button("Edit Patch", width="stretch", key=edit_key)
        reject_clicked = st.button("Reject Action", width="stretch", key=reject_key)

        if edit_clicked:
            st.rerun()

        if approve_clicked:
            resume_command = _resume_pending_action(pending_action, edited_new_snippet, "approve")
            st.session_state.pending_action = None
            resumed_action = _run_session(
                stack_trace,
                provider,
                model_name,
                thread_id,
                command=resume_command,
                pending_action_override={**pending_action, "new_snippet": edited_new_snippet},
            )
            st.session_state.pending_action = resumed_action
            if resumed_action is None:
                st.success("Execution completed.")
            else:
                st.rerun()

        if reject_clicked:
            reject_command = _resume_pending_action(pending_action, edited_new_snippet, "reject")
            st.session_state.pending_action = None
            resumed_action = _run_session(
                stack_trace,
                provider,
                model_name,
                thread_id,
                command=reject_command,
            )
            st.session_state.pending_action = resumed_action
            st.rerun()


def _render_message(message: dict[str, Any]) -> None:
    message_type = message.get("type", "Unknown")
    content = message.get("content", "")
    tool_calls = message.get("tool_calls") or []
    action_label, action_class = _tool_category(message.get("name"))

    if message_type == "HumanMessage":
        with st.expander("Input stack trace", expanded=True):
            st.code(content, language="text")
        return

    if message_type == "AIMessage" and tool_calls:
        with st.expander("Tool invocation", expanded=True):
            for index, tool_call in enumerate(tool_calls, start=1):
                call_label, call_class = _tool_category(tool_call.get("name"))
                st.markdown(
                    f"<div class='tool-box'>{_badge_html(call_label, call_class)} Tool {index}: {tool_call.get('name', 'unknown')}</div>",
                    unsafe_allow_html=True,
                )
                st.code(_format_yaml(tool_call), language="yaml")
        return

    if message_type == "ToolMessage":
        with st.expander(f"{action_label}: {message.get('name', 'tool')}", expanded=True):
            st.markdown(f"<div class='observation-box'>{_badge_html(action_label, action_class)} Raw tool output</div>", unsafe_allow_html=True)
            st.code(content, language="text")
        return

    if message_type == "AIMessage":
        with st.expander("Root cause analysis and patch", expanded=True):
            st.markdown("<div class='success-box'>Final response</div>", unsafe_allow_html=True)
            st.markdown(content)
        return

    with st.expander(f"{message_type}", expanded=True):
        st.code(content, language="text")


st.set_page_config(page_title="TraceMind — AI Debugging Agent", layout="wide")
st.markdown(
    """
    <style>
    .main-title {
        font-size: 3rem;
        font-weight: 800;
        letter-spacing: -0.04em;
        line-height: 1.05;
        margin-bottom: 0.25rem;
        background: linear-gradient(120deg, #0f172a 0%, #1d4ed8 55%, #0f766e 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .subtitle {
        color: #475569;
        font-size: 1rem;
        margin-bottom: 1.25rem;
    }
    .tool-box, .observation-box, .success-box {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        border-radius: 0.75rem;
        padding: 0.75rem 1rem;
        margin-bottom: 0.5rem;
        border: 1px solid rgba(15, 23, 42, 0.12);
    }
    .action-badge {
        display: inline-block;
        border-radius: 999px;
        padding: 0.2rem 0.55rem;
        margin-right: 0.6rem;
        font-size: 0.72rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .read-action {
        background: #dbeafe;
        color: #1d4ed8;
    }
    .write-action {
        background: #ffedd5;
        color: #c2410c;
    }
    .neutral-action {
        background: #e2e8f0;
        color: #475569;
    }
    .tool-box {
        background: #eff6ff;
        color: #1d4ed8;
    }
    .observation-box {
        background: #f8fafc;
        color: #334155;
    }
    .success-box {
        background: #ecfdf5;
        color: #047857;
    }
    .stButton button {
        background: linear-gradient(120deg, #0f172a 0%, #1d4ed8 100%);
        color: white;
        border: none;
        border-radius: 0.75rem;
        padding: 0.75rem 1.25rem;
        font-weight: 700;
    }
    .stTextArea textarea {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        min-height: 320px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown("<div class='main-title'>TraceMind — AI Debugging Agent</div>", unsafe_allow_html=True)
st.markdown(
    "<div class='subtitle'>Paste a traceback and let the agent inspect the local project, trace cross-file dependencies, and produce a targeted patch.</div>",
    unsafe_allow_html=True,
)

if "thread_id" not in st.session_state:
    st.session_state.thread_id = ""
if "pending_action" not in st.session_state:
    st.session_state.pending_action = None
if "last_stack_trace" not in st.session_state:
    st.session_state.last_stack_trace = SAMPLE_TRACEBACK
if "telemetry_history" not in st.session_state:
    st.session_state.telemetry_history = []

with st.sidebar:
    st.header("AI Provider & Model")
    selected_label = st.selectbox(
        "Select a provider",
        [option["label"] for option in PROVIDER_OPTIONS],
        index=1,
    )
    selected_option = next(option for option in PROVIDER_OPTIONS if option["label"] == selected_label)
    st.caption(f"Expected key: {selected_option['key_name']}")
    st.caption(f"Model: {selected_option['model']}")
    
    st.divider()
    
    st.header("Execution Mode")
    execution_mode = st.segmented_control(
        "Select mode",
        ["Local Demo Mode", "Repository Sandbox Mode"],
        default="Local Demo Mode",
        label_visibility="collapsed"
    )

tab_workspace, tab_telemetry = st.tabs(["Debug Workspace", "Telemetry & Analytics"])

with tab_workspace:
    left_column, right_column = st.columns([1, 1.2], gap="large")

    with left_column:
        st.subheader("Input Configuration")
        if execution_mode == "Local Demo Mode":
            stack_trace = st.text_area("Runtime error log", value=st.session_state.last_stack_trace, height=360)
            repo_dir = str(PROJECT_ROOT)
            test_command = ""
        else:
            repo_dir = st.text_input("Local Repository Path or Git Clone URL", value=str(PROJECT_ROOT))
            test_command = st.text_input("Test Command or Test File to Run", value="python sample_bug/main.py")
            stack_trace_input = st.text_area("Optional Stack Trace (leave blank to auto-run tests in sandbox)", value="", height=200)
            stack_trace = stack_trace_input
            
        start_analysis = st.button("Start Autonomous Analysis", type="primary", width="stretch")

    with right_column:
        st.subheader("Agent Reasoning & Resolution")
        output_slot = st.container()

with tab_telemetry:
    st.subheader("Swarm Telemetry & Metrics")
    if st.session_state.telemetry_history:
        latest = st.session_state.telemetry_history[-1]
        
        # Display key metrics in cards
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Elapsed Time", f"{latest['elapsed_time']}s")
        c2.metric("Active Sub-Agent", latest["active_agent"])
        c3.metric("Swarm Step", latest["step_count"])
        c4.metric("Est. Tokens Used", latest["estimated_tokens"])
        
        # Display route tracing log
        st.write("---")
        st.subheader("Sub-Agent Route Tracing")
        for tel in st.session_state.telemetry_history:
            st.markdown(f"**Step {tel['step_count']} ({tel['elapsed_time']}s)**: Routing target `{tel['active_agent']}` (Estimated tokens: {tel['estimated_tokens']})")
    else:
        st.info("No active telemetry records. Start an analysis to stream live agent swarm metrics.")

if start_analysis:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.telemetry_history = []  # Reset telemetry log for new session
    resolved_repo_dir = _clone_if_git_url(repo_dir)
    st.session_state.resolved_repo_dir = resolved_repo_dir
    st.session_state.test_command = test_command
    
    if execution_mode == "Repository Sandbox Mode" and not stack_trace.strip():
        with st.spinner("Executing tests inside sandbox to capture initial traceback..."):
            from tools import run_tests_in_sandbox
            sandbox_output = run_tests_in_sandbox.invoke({
                "repo_dir": resolved_repo_dir,
                "test_command": test_command
            })
            stack_trace = sandbox_output
            
    st.session_state.last_stack_trace = stack_trace
    session_provider = (
        "offline-demo"
        if execution_mode == "Local Demo Mode"
        else selected_option["provider"]
    )
    st.session_state.pending_action = _run_session(
        stack_trace,
        session_provider,
        selected_option["model"],
        st.session_state.thread_id,
    )

if st.session_state.pending_action:
    with tab_workspace:
        _render_approval_card(
            st.session_state.pending_action,
            (
                "offline-demo"
                if execution_mode == "Local Demo Mode"
                else selected_option["provider"]
            ),
            selected_option["model"],
            st.session_state.thread_id,
            stack_trace,
        )
else:
    if st.session_state.thread_id and "resolved_repo_dir" in st.session_state:
        resolved_dir = st.session_state.resolved_repo_dir
        try:
            import git
            repo = git.Repo(resolved_dir)
            diff_text = repo.git.diff()
            if not diff_text and repo.head.commit:
                base_branch = "main" if "main" in repo.heads else "master"
                diff_text = repo.git.diff(base_branch)
            
            if diff_text:
                with tab_workspace:
                    st.subheader("Pull Request Patch")
                    st.code(diff_text, language="diff")
                    st.download_button(
                        label="Download PR Patch (.patch)",
                        data=diff_text,
                        file_name="tracemind_pr.patch",
                        mime="text/plain",
                        width="stretch"
                    )
        except Exception as e:
            pass
