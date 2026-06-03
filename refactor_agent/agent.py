"""
RefactorAgent — uses Claude claude-sonnet-4-6 with tool use to detect code smells and
propose/apply refactors.  Diffs are shown before any file is written.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

from .smells import CodeSmell, DetectionResult, SmellDetector


# ---------------------------------------------------------------------------
# Tool definitions (Claude tool-use schema)
# ---------------------------------------------------------------------------

_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read the full text of a Python source file.  "
            "Returns the file contents as a string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the Python file.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write new content to a Python source file, replacing its current content.  "
            "Always show the user a diff and obtain confirmation before calling this tool "
            "unless the caller has already approved the change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "New file content (complete, not a patch).",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List Python files in a directory, optionally filtered by a glob pattern.  "
            "Returns a JSON array of matching file paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory to search (default: current directory).",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to *directory* (default: **/*.py).",
                },
            },
            "required": ["directory"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution helpers
# ---------------------------------------------------------------------------

def _execute_read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"ERROR: File not found: {path}"
    if not p.is_file():
        return f"ERROR: Path is not a file: {path}"
    try:
        return p.read_text(encoding="utf-8")
    except Exception as exc:
        return f"ERROR reading file: {exc}"


def _execute_list_files(directory: str, pattern: str = "**/*.py") -> str:
    d = Path(directory)
    if not d.exists():
        return json.dumps([f"ERROR: Directory not found: {directory}"])
    matches = sorted(str(p) for p in d.glob(pattern) if p.is_file())
    return json.dumps(matches)


def _diff(original: str, revised: str, path: str) -> str:
    """Return a unified diff string between *original* and *revised*."""
    lines_original = original.splitlines(keepends=True)
    lines_revised = revised.splitlines(keepends=True)
    diff = difflib.unified_diff(
        lines_original,
        lines_revised,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# RefactorAgent
# ---------------------------------------------------------------------------

class RefactorAgent:
    """An AI agent that detects code smells and refactors Python source files.

    Parameters
    ----------
    api_key:
        Anthropic API key.  Falls back to the ``ANTHROPIC_API_KEY`` env-var.
    model:
        Claude model to use.  Defaults to ``claude-sonnet-4-6``.
    dry_run:
        When *True* the agent proposes refactors and shows diffs but never
        writes files.  The caller must explicitly pass ``dry_run=False`` to
        allow writes.
    confirm_writes:
        When *True* (default) the agent prompts the user on stdout/stdin
        before each file write.  Set to *False* to apply all suggested
        refactors automatically (still respects *dry_run*).
    max_iterations:
        Safety cap on the number of Claude ↔ tool-call round-trips per
        ``run()`` call.  Default is 20.
    """

    MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        dry_run: bool = False,
        confirm_writes: bool = True,
        max_iterations: int = 20,
    ) -> None:
        self.model = model
        self.dry_run = dry_run
        self.confirm_writes = confirm_writes
        self.max_iterations = max_iterations
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self._detector = SmellDetector()
        # Track files that were actually written this session.
        self._written_files: List[str] = []
        # Pending write callbacks keyed by path (used when confirm_writes=True).
        self._pending_writes: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def written_files(self) -> List[str]:
        """Files written (or that *would* be written in dry-run mode) this session."""
        return list(self._written_files)

    def scan(self, path: str, smell_types: Optional[List[str]] = None) -> DetectionResult:
        """Run the AST-based smell detector on *path* (file or directory).

        Parameters
        ----------
        path:
            File or directory to scan.
        smell_types:
            Optional list of smell types to include.  Pass *None* to include all.
        """
        p = Path(path)
        results = DetectionResult()
        if p.is_file():
            results = self._detector.analyse_file(p)
        elif p.is_dir():
            for py_file in sorted(p.rglob("*.py")):
                file_result = self._detector.analyse_file(py_file)
                results.smells.extend(file_result.smells)
        else:
            raise FileNotFoundError(f"Path not found: {path}")

        if smell_types:
            results.smells = [s for s in results.smells if s.smell_type in smell_types]
        return results

    def run(
        self,
        task: str,
        context: Optional[str] = None,
    ) -> str:
        """Send *task* to Claude and run the tool-use agentic loop.

        Parameters
        ----------
        task:
            Natural-language description of what to do (e.g.
            ``"Refactor long methods in src/"``).  Pre-detected smells can be
            included here or passed via *context*.
        context:
            Optional additional context to prepend to the system prompt
            (e.g. a formatted detection report).

        Returns
        -------
        str
            Claude's final text response.
        """
        system = self._build_system_prompt(context)
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": task}
        ]

        for _iteration in range(self.max_iterations):
            response = self._client.messages.create(
                model=self.model,
                max_tokens=8192,
                system=system,
                tools=_TOOLS,
                messages=messages,
            )

            # Append assistant turn.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Extract and return final text.
                return self._extract_text(response.content)

            if response.stop_reason == "tool_use":
                tool_results = self._handle_tool_calls(response.content)
                messages.append({"role": "user", "content": tool_results})
                continue

            # Any other stop reason — return whatever text is available.
            return self._extract_text(response.content)

        return "Agent reached the maximum number of iterations without finishing."

    def refactor(
        self,
        path: str,
        smell_types: Optional[List[str]] = None,
    ) -> str:
        """Convenience method: scan *path*, then ask Claude to refactor found smells.

        Parameters
        ----------
        path:
            File or directory to scan and refactor.
        smell_types:
            Limit refactoring to these smell types.  Pass *None* for all.
        """
        detection = self.scan(path, smell_types=smell_types)
        if not detection:
            return "No code smells detected — nothing to refactor."

        smell_report = detection.summary()
        task = (
            f"I've scanned the codebase at '{path}' and found the following code smells:\n\n"
            f"{smell_report}\n\n"
            "Please refactor the affected files to address these issues.  For each file:\n"
            "1. Read the current content with read_file.\n"
            "2. Propose the refactored content.\n"
            "3. Write the updated file with write_file (diffs will be shown automatically).\n"
            "Preserve all existing behaviour; only restructure the code."
        )
        return self.run(task, context=smell_report)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system_prompt(context: Optional[str]) -> str:
        base = (
            "You are an expert Python refactoring assistant.  "
            "You have access to tools that let you read files, list files in a directory, "
            "and write refactored content back to disk.\n\n"
            "Guidelines:\n"
            "- Before writing any file, produce a clear explanation of the changes you are making.\n"
            "- Keep the public API and behaviour identical; only improve structure.\n"
            "- Address one smell at a time when possible so diffs stay readable.\n"
            "- If you see additional issues beyond those requested, mention them but don't \n"
            "  fix them unless explicitly asked.\n"
            "- When you have finished all changes, provide a concise summary of what was done."
        )
        if context:
            base += f"\n\nDetection report:\n{context}"
        return base

    def _handle_tool_calls(
        self, content_blocks: List[Any]
    ) -> List[Dict[str, Any]]:
        """Execute each tool_use block and return a list of tool_result messages."""
        tool_results: List[Dict[str, Any]] = []
        for block in content_blocks:
            if block.type != "tool_use":
                continue
            result_content = self._dispatch_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_content,
                }
            )
        return tool_results

    def _dispatch_tool(self, name: str, tool_input: Dict[str, Any]) -> str:
        """Route a tool call to the appropriate handler."""
        if name == "read_file":
            return _execute_read_file(tool_input["path"])

        if name == "list_files":
            return _execute_list_files(
                tool_input["directory"],
                tool_input.get("pattern", "**/*.py"),
            )

        if name == "write_file":
            return self._execute_write_file(
                tool_input["path"], tool_input["content"]
            )

        return f"ERROR: Unknown tool '{name}'"

    def _execute_write_file(self, path: str, new_content: str) -> str:
        """Handle a write_file tool call, showing a diff and optionally confirming."""
        p = Path(path)

        # Build diff against current file (or empty string if new).
        original = p.read_text(encoding="utf-8") if p.exists() else ""
        diff_text = _diff(original, new_content, path)

        if not diff_text:
            return f"No changes detected for {path} — file not written."

        # Always print the diff so the user can see what would change.
        print(f"\n{'='*70}")
        print(f"DIFF for {path}:")
        print("="*70)
        print(diff_text)
        print("="*70)

        if self.dry_run:
            self._written_files.append(path)
            return f"[dry-run] Would write {path} (see diff above)."

        if self.confirm_writes:
            answer = input(f"Apply changes to {path}? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                return f"User declined to apply changes to {path}."

        # Write the file.
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_content, encoding="utf-8")
        self._written_files.append(path)
        return f"Successfully wrote {path}."

    @staticmethod
    def _extract_text(content_blocks: List[Any]) -> str:
        """Concatenate all text blocks from an assistant response."""
        parts = []
        for block in content_blocks:
            if hasattr(block, "type") and block.type == "text":
                parts.append(block.text)
        return "\n".join(parts).strip()
