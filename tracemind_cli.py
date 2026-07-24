#!/usr/bin/env python3
"""Headless CLI entry point for TraceMind CI/CD integration."""

import argparse
import os
import sys
from pathlib import Path
from agent import run_debugging_session
from langgraph.types import Command
import git

def parse_args():
    parser = argparse.ArgumentParser(description="TraceMind CI/CD Autofix CLI")
    parser.add_argument(
        "--traceback-file",
        type=str,
        help="Path to a file containing the error stack trace/logs."
    )
    parser.add_argument(
        "--traceback",
        type=str,
        help="Direct traceback string to resolve."
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        default=".",
        help="Path to the target repository directory."
    )
    parser.add_argument(
        "--output-patch",
        type=str,
        default="tracemind_fix.patch",
        help="Path where the generated .patch file will be saved."
    )
    return parser.parse_args()


def run_headless_session(stack_trace: str, repo_dir: str, patch_path: str):
    print("--- Initializing Headless TraceMind Session ---")
    print(f"Target Repo: {os.path.abspath(repo_dir)}")
    
    # 1. Run the initial agent workflow
    events = list(run_debugging_session(stack_trace))
    pending_action = None
    thread_id = None
    
    for event in events:
        ev_type = event.get("event")
        step = event.get("step")
        if ev_type == "session_started":
            thread_id = event.get("thread_id")
            print(f"[Session Started] Thread ID: {thread_id}")
        elif ev_type == "state":
            messages = event.get("messages", [])
            if messages:
                latest = messages[-1]
                print(f"[Step {step}] Active Agent: {event.get('telemetry', {}).get('active_agent', 'Supervisor')}")
                print(f"  Message type: {latest.get('type')}")
        elif ev_type == "interrupted":
            pending_action = event.get("pending_action")
            print(f"\n[INTERRUPT] Proposed Action: {pending_action.get('tool_name')}")
            print(f"  Target File: {pending_action.get('file_path')}")
            
    # 2. If the session paused for approval, auto-approve it programmatically
    while pending_action:
        print("\n[CI AUTO-APPROVE] Approving proposed patch and resuming...")
        resume_cmd = Command(resume=True)
        resume_events = list(run_debugging_session(
            stack_trace,
            thread_id=thread_id,
            command=resume_cmd,
            pending_action_override=pending_action
        ))
        
        pending_action = None  # Reset
        for event in resume_events:
            ev_type = event.get("event")
            step = event.get("step")
            if ev_type == "state":
                messages = event.get("messages", [])
                if messages:
                    latest = messages[-1]
                    print(f"[Resume Step {step}] Message: {latest.get('type')}")
            elif ev_type == "interrupted":
                pending_action = event.get("pending_action")
                print(f"\n[INTERRUPT] Proposed Action: {pending_action.get('tool_name')}")
                
    # 3. Fetch git diff and write out the patch file
    try:
        repo = git.Repo(repo_dir)
        base_branch = "main" if "main" in repo.heads else "master"
        diff_text = repo.git.diff(base_branch)
        if not diff_text and repo.head.commit:
            diff_text = repo.git.diff("HEAD~1")
            
        if diff_text:
            Path(patch_path).write_text(diff_text, encoding="utf-8")
            print(f"\n[SUCCESS] Unified git diff saved to: {os.path.abspath(patch_path)}")
            print("==================== GIT DIFF ====================")
            print(diff_text)
            print("==================================================")
        else:
            print("\n[WARNING] Executed successfully but no changes were detected in Git.")
    except Exception as e:
        print(f"\n[ERROR] Failed to extract git diff/patch: {e}")
        
    print("\n--- Headless TraceMind Session Completed ---")


def main():
    args = parse_args()
    stack_trace = ""
    
    if args.traceback:
        stack_trace = args.traceback
    elif args.traceback_file:
        traceback_path = Path(args.traceback_file)
        if traceback_path.exists():
            stack_trace = traceback_path.read_text(encoding="utf-8", errors="replace")
            print(f"Loaded traceback from file: {traceback_path}")
        else:
            print(f"ERROR: Traceback file not found at {args.traceback_file}")
            sys.exit(1)
    else:
        # Check standard input (piped logs)
        if not sys.stdin.isatty():
            stack_trace = sys.stdin.read()
            print("Loaded traceback from standard input pipeline.")
            
    if not stack_trace.strip():
        print("ERROR: No traceback input provided. Use --traceback, --traceback-file, or pipe input.")
        sys.exit(1)
        
    run_headless_session(stack_trace, args.repo_dir, args.output_patch)


if __name__ == "__main__":
    main()
