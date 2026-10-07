# TraceMind

TraceMind is an AI-assisted debugging agent that analyzes runtime errors, explores a local polyglot codebase, proposes targeted code changes, verifies those changes in a sandbox, and produces a Git diff suitable for review.

The project provides:

- A Streamlit web interface for interactive debugging.
- A LangGraph/LangChain ReAct workflow for repository inspection and patch verification.
- A deterministic offline demo mode that does not require an AI provider.
- A headless CLI for CI/CD automation.
- A GitHub Actions workflow that can generate an autofix pull request after a failed workflow or a `bug` issue label.

## Tech stack

- **Language:** Python 3.10+ (the local development environment uses Python 3.13).
- **UI:** Streamlit.
- **Agent orchestration:** LangGraph.
- **LLM integration:** LangChain Core and LangChain OpenAI-compatible chat clients.
- **Supported providers:** OpenAI, Google Gemini, Groq, GitHub Models, and local Ollama.
- **Repository analysis:** Deterministic filesystem tools, Tree-sitter AST parsing, and cross-file text search.
- **Execution and verification:** Python subprocess execution with timeouts and optional Docker sandbox execution.
- **Version control:** GitPython and Git branches for isolated patches.
- **Automation:** GitHub Actions and `peter-evans/create-pull-request`.

## API and provider configuration

TraceMind uses OpenAI-compatible chat APIs for the hosted providers. API keys are read from environment variables and are never stored in this README or source code.

| Provider | Environment variable | Example model |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` |
| Google Gemini | `GOOGLE_API_KEY` | `gemini-2.5-flash` |
| Groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| GitHub Models | `GITHUB_TOKEN` | Provider-supported model |
| Ollama | No hosted key required; optional `OLLAMA_API_KEY` | `qwen2.5-coder:7b` |

Optional environment variables:

- `TRACE_MIND_PROVIDER`: Default provider when running outside the UI.
- `TRACE_MIND_MODEL`: Default model when running outside the UI.
- `TRACE_MIND_BASE_URL`: Override the OpenAI-compatible endpoint. This is useful for Groq, GitHub Models, or Ollama-compatible services.

Create a local `.env` file or export variables in the shell. Keep credentials out of Git. The committed project should contain only variable names and placeholders, never real key values.

### Offline demo mode

Select **Local Demo Mode** in the Streamlit sidebar to run the built-in deterministic workflow without an API key. This mode uses the sample project and demonstrates repository inspection, patch approval, and verification.

## Dependencies

Runtime and development dependencies are listed in [`requirements.txt`](./requirements.txt):

- `langchain`, `langchain-core`, `langchain-openai`
- `langchain-groq`, `langchain-ollama`
- `langgraph`
- `streamlit`
- `pydantic`
- `python-dotenv`
- `GitPython`
- `docker`
- `datasets`
- `tree-sitter`
- `tree-sitter-languages` for Python versions below 3.13
- `tree-sitter-language-pack` for Python 3.13 and newer

Docker is optional for local development. If Docker is unavailable, verification falls back to local subprocess execution with a timeout.

## Installation

From the `TraceMind-Agent` directory:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

On Windows, activate the environment with:

```powershell
venv\Scripts\Activate.ps1
```

## Running the Streamlit app

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501`.

### Interactive workflow

1. Select an AI provider and model, or choose **Local Demo Mode**.
2. Paste a traceback in **Debug Workspace**.
3. Select **Start Autonomous Analysis**.
4. TraceMind inspects the project structure and relevant source code.
5. Before changing files, the UI displays an approval card with the proposed diff.
6. Approve, edit, or reject the patch.
7. The agent runs verification and displays the resulting diff and telemetry.

For **Repository Sandbox Mode**, provide a local repository path or Git URL and a test command. If no traceback is supplied, TraceMind runs the command first to collect the initial failure output.

## CLI usage

The headless entry point is [`tracemind_cli.py`](./tracemind_cli.py):

```bash
python tracemind_cli.py \
  --traceback-file traceback.log \
  --repo-dir /path/to/repository \
  --output-patch tracemind_fix.patch
```

A traceback can also be passed directly:

```bash
python tracemind_cli.py \
  --traceback "KeyError: 'price' in calculator.py" \
  --repo-dir /path/to/repository
```

The CLI auto-approves proposed patches for CI use, runs the verification workflow, and writes a unified Git diff when changes are detected.

## GitHub Actions automation

The workflow in [`.github/workflows/tracemind-autofix.yml`](./.github/workflows/tracemind-autofix.yml) can run when:

- A workflow named `CI Tests` completes unsuccessfully.
- An issue receives the `bug` label.

Configure the required provider credential as a GitHub Actions repository secret, for example `GOOGLE_API_KEY` or `OPENAI_API_KEY`. The workflow passes the secret to the process at runtime; no secret belongs in the repository.

The workflow installs dependencies, extracts a traceback, runs the CLI, and opens a pull request when a patch file is generated.

## Project structure

```text
TraceMind-Agent/
├── app.py                         # Streamlit web application
├── agent.py                       # LangGraph workflow and provider setup
├── tools.py                       # Filesystem, AST, patch, Git, and test tools
├── tracemind_cli.py               # Headless CLI and CI entry point
├── requirements.txt               # Python dependencies
├── docker/
│   └── Dockerfile.sandbox         # Optional sandbox image for test execution
├── sample_bug/
│   ├── main.py                    # Python sample used by the demo
│   └── utils/
│       └── calculator.py           # Python sample dependency
├── sample_bug_ts/
│   ├── main.ts                    # TypeScript sample
│   └── utils/
│       └── helper.ts               # TypeScript sample dependency
├── random_js_project/
│   ├── app.js                     # JavaScript sample project
│   ├── utils.js                   # JavaScript helper
│   └── package.json               # JavaScript package metadata
├── benchmarks/
│   ├── run_swe_bench.py           # Benchmark runner
│   └── results.json               # Benchmark output
├── .github/
│   └── workflows/
│       └── tracemind-autofix.yml  # GitHub Actions autofix workflow
├── .gitignore                     # Ignore rules for secrets and generated files
└── README.md                      # Project documentation
```

Local-only directories such as `venv/`, `__pycache__/`, `.git/`, and `.env` are intentionally excluded from the structure above and should not be committed.

## Safety and limitations

- Review every proposed patch before merging it.
- Use a least-privilege token for hosted model access and GitHub automation.
- Do not put API keys in source files, README files, traceback input, or pull request descriptions.
- Docker sandbox isolation is preferred, but local fallback execution is available when Docker is not running.
- Tree-sitter analysis currently targets Python, JavaScript, TypeScript, Java, and Go file extensions.
