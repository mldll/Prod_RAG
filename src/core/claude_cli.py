"""claude_local: answers from the `claude` CLI on this machine (Claude Code, signed in with your own subscription).

For local runs only: create_llm() refuses it in the clouds, in CI, in a container and with a cloud state store
(blocked_reason()). Every call runs `claude --print` in an empty folder with every tool, MCP server, settings file
(they bring your CLAUDE.md), slash command and saved session turned off, so text from documents or questions can't
make it act, and your own Claude Code instructions never reach the model. The prompt goes in on stdin.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from src.config import CLAUDE_LOCAL_EFFORT, CLAUDE_LOCAL_MAX_CONCURRENCY, CLAUDE_LOCAL_TIMEOUT_SECONDS

# Replaces Claude Code's own system prompt (a coding agent's, about 3,000 tokens): the app's prompts carry the task
SYSTEM_PROMPT = "Follow the user's instructions exactly. Reply in plain text."
SANDBOX_DIR = os.path.join(tempfile.gettempdir(), "prod_rag_claude_sandbox")  # the empty working folder of every call
# Without it, a console program started from a windowless process opens a window for every call
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Left out of the CLI's environment: a surrounding Claude Code session's own variables; ANTHROPIC_* (an
# ANTHROPIC_API_KEY would switch the CLI from your subscription to API billing); and secrets the CLI doesn't need
_SESSION_VARS = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION"})
_DROPPED_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_", "AZURE_", "AWS_")
_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CONNECTION_STRING")

# Each call is its own CLI process (about 2.7 s); calls beyond this many wait for a free slot
_slots = threading.BoundedSemaphore(CLAUDE_LOCAL_MAX_CONCURRENCY)


class ClaudeCliError(Exception):
    """The CLI call failed. status_code 429 marks a usage or rate limit, which /query answers with 503."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def blocked_reason(environ=None, state_backend: str | None = None, in_container: bool | None = None) -> str | None:
    """Why claude_local can't run here, or None on a developer machine.

    It answers with your own subscription login, which is for your own use: never for the public site's visitors,
    never in CI, and never for anything that writes to the clouds' state stores.
    """
    from src import config
    environ = os.environ if environ is None else environ
    state_backend = config.STATE_BACKEND if state_backend is None else state_backend
    if in_container is None:
        in_container = os.path.exists("/.dockerenv")
    if environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return "this process runs on AWS Lambda"
    if environ.get("CONTAINER_APP_NAME"):
        return "this process runs on Azure Container Apps"
    if environ.get("GITHUB_ACTIONS") or environ.get("CI"):
        return "this process runs in CI"
    if in_container:
        return "this process runs in a container"
    if state_backend != "local":
        return f"STATE_BACKEND is {state_backend!r}, so its results could reach a cloud state store"
    return None


def clean_env(environ=None) -> dict[str, str]:
    """The environment for the CLI: everything except session variables, ANTHROPIC_*, cloud settings and secrets."""
    environ = os.environ if environ is None else environ
    return {name: value for name, value in environ.items()
            if name not in _SESSION_VARS and not name.startswith(_DROPPED_PREFIXES)
            and not any(marker in name.upper() for marker in _SECRET_MARKERS)}


def command(executable: str, model: str, effort: str | None, json_schema: dict | None = None) -> list[str]:
    """The CLI's command line. The prompt isn't in it: it goes in on stdin."""
    cmd = [executable, "--print", "--output-format", "json", "--model", model, "--system-prompt", SYSTEM_PROMPT,
           "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
           "--no-session-persistence", "--permission-mode", "dontAsk"]
    if effort:
        cmd += ["--effort", effort]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    return cmd


def parse_result(stdout: str, returncode: int, stderr: str) -> dict:
    """The result object that `claude --print --output-format json` prints, or ClaudeCliError."""
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).strip()[:300]
        raise ClaudeCliError(f"claude exited {returncode} without a JSON result: {detail}") from None
    if not isinstance(result, dict):
        raise ClaudeCliError("claude printed JSON that isn't a result object")
    if result.get("is_error") or result.get("subtype") != "success":
        detail = str(result.get("result") or "")[:300]
        status = result.get("api_error_status")
        limited = status == 429 or "limit" in detail.lower()
        raise ClaudeCliError(f"claude reported {result.get('subtype')}: {detail}",
                             status_code=429 if limited else status if isinstance(status, int) else None)
    return result


class ChatClaudeCLI(BaseChatModel):
    """A LangChain chat model over the claude CLI: invoke() and with_structured_output() work like ChatGroq's."""

    model_name: str = "haiku"
    effort: str | None = CLAUDE_LOCAL_EFFORT
    timeout: float = CLAUDE_LOCAL_TIMEOUT_SECONDS
    executable: str | None = None  # None: claude on PATH
    sandbox: str = SANDBOX_DIR

    @property
    def _llm_type(self) -> str:
        return "claude-cli"

    @property
    def _identifying_params(self) -> dict:
        return {"model_name": self.model_name, "effort": self.effort}

    def _generate(self, messages, stop=None, run_manager=None, json_schema=None, **kwargs):
        # The app sends one prompt string; several messages are joined into one prompt
        prompt = "\n\n".join(str(message.content) for message in messages)
        result = self._run(prompt, json_schema)
        if json_schema is not None and result.get("structured_output") is None:
            raise ClaudeCliError("claude returned no structured output")
        usage = result.get("usage") or {}
        input_tokens = sum(usage.get(name) or 0
                           for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        output_tokens = usage.get("output_tokens") or 0
        content = (json.dumps(result["structured_output"]) if json_schema is not None
                   else str(result.get("result") or ""))
        message = AIMessage(
            content=content,
            response_metadata={"finish_reason": result.get("stop_reason"),
                               "model_name": next(iter(result.get("modelUsage") or {}), self.model_name)},
            usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        """A runnable that returns a validated `schema` instance; the CLI enforces the schema (--json-schema)."""
        if include_raw:
            raise NotImplementedError("claude_local doesn't support include_raw")
        return self.bind(json_schema=schema.model_json_schema()) | RunnableLambda(
            lambda message: schema.model_validate_json(message.content))

    def _run(self, prompt: str, json_schema: dict | None) -> dict:
        executable = self.executable or shutil.which("claude")
        if executable is None:
            raise ClaudeCliError("The claude CLI isn't on PATH: install Claude Code and sign in by running `claude`")
        os.makedirs(self.sandbox, exist_ok=True)
        with _slots:
            try:
                process = subprocess.run(
                    command(executable, self.model_name, self.effort, json_schema), input=prompt,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=self.sandbox,
                    env=clean_env(), timeout=self.timeout, check=False, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired as e:
                raise ClaudeCliError(f"claude timed out after {self.timeout:.0f} s") from e
            except OSError as e:
                raise ClaudeCliError(f"Can't run {executable}: {e}") from e
        return parse_result(process.stdout, process.returncode, process.stderr)
