"""
SmellDetector — AST-based code smell detection.

Detects:
  - long methods        (functions / methods exceeding a configurable line threshold)
  - deep nesting        (nested control-flow exceeding a configurable depth threshold)
  - duplicate blocks    (repeated statement sequences using a rolling-hash approach)
"""

from __future__ import annotations

import ast
import hashlib
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CodeSmell:
    """A single detected code smell."""

    smell_type: str          # "long_method" | "deep_nesting" | "duplicate_block"
    file_path: str
    line_start: int
    line_end: int
    name: Optional[str]      # function / class name, if applicable
    description: str
    severity: str            # "low" | "medium" | "high"

    def __str__(self) -> str:
        loc = f"{self.file_path}:{self.line_start}"
        if self.line_end != self.line_start:
            loc += f"-{self.line_end}"
        tag = f"[{self.smell_type}]"
        name_part = f" ({self.name})" if self.name else ""
        return f"{tag}{name_part} {loc} — {self.description}"


@dataclass
class DetectionResult:
    """Aggregated results from analysing one or more files."""

    smells: List[CodeSmell] = field(default_factory=list)

    def by_type(self, smell_type: str) -> List[CodeSmell]:
        return [s for s in self.smells if s.smell_type == smell_type]

    def __bool__(self) -> bool:
        return bool(self.smells)

    def summary(self) -> str:
        if not self.smells:
            return "No smells detected."
        lines = [f"Detected {len(self.smells)} smell(s):", ""]
        for smell in self.smells:
            lines.append(f"  {smell}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_lines(node: ast.AST) -> int:
    """Return the number of source lines spanned by *node*."""
    try:
        return node.end_lineno - node.lineno + 1  # type: ignore[attr-defined]
    except AttributeError:
        return 0


def _max_nesting_depth(node: ast.AST) -> int:
    """Recursively compute the maximum nesting depth of control-flow inside *node*."""
    _NESTING_NODES = (
        ast.If, ast.For, ast.While, ast.With, ast.Try,
        ast.AsyncFor, ast.AsyncWith,
    )

    def _depth(n: ast.AST, current: int) -> int:
        if isinstance(n, _NESTING_NODES):
            current += 1
        child_depths = [_depth(child, current) for child in ast.iter_child_nodes(n)]
        return max(child_depths, default=current)

    return _depth(node, 0)


def _stmt_hash(stmt: ast.stmt, source_lines: List[str]) -> str:
    """Return a stable hash for the source text of *stmt*."""
    try:
        text = "\n".join(
            source_lines[stmt.lineno - 1 : stmt.end_lineno]  # type: ignore[attr-defined]
        )
    except (AttributeError, IndexError):
        text = ast.dump(stmt)
    # Strip leading whitespace so indentation differences don't matter.
    text = textwrap.dedent(text).strip()
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324 — not cryptographic


# ---------------------------------------------------------------------------
# Main detector class
# ---------------------------------------------------------------------------

class SmellDetector:
    """Detects code smells in Python source files using AST analysis.

    Parameters
    ----------
    long_method_threshold:
        Minimum number of lines for a function/method to be considered "long".
        Default is 20.
    nesting_threshold:
        Minimum nesting depth to flag as "deep nesting".
        Default is 4.
    duplicate_block_min_lines:
        Minimum number of consecutive statements (by line count) to consider
        when looking for duplicate blocks.  Default is 5.
    """

    def __init__(
        self,
        long_method_threshold: int = 20,
        nesting_threshold: int = 4,
        duplicate_block_min_lines: int = 5,
    ) -> None:
        self.long_method_threshold = long_method_threshold
        self.nesting_threshold = nesting_threshold
        self.duplicate_block_min_lines = duplicate_block_min_lines

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse_file(self, path: Path | str) -> DetectionResult:
        """Analyse a single Python source file and return all detected smells."""
        path = Path(path)
        source = path.read_text(encoding="utf-8")
        return self.analyse_source(source, file_path=str(path))

    def analyse_source(
        self, source: str, file_path: str = "<string>"
    ) -> DetectionResult:
        """Analyse raw Python source text."""
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as exc:
            # Return an empty result rather than crashing.
            return DetectionResult()

        source_lines = source.splitlines()
        result = DetectionResult()

        result.smells.extend(self.detect_long_methods(tree, file_path))
        result.smells.extend(self.detect_deep_nesting(tree, file_path))
        result.smells.extend(
            self.detect_duplicate_blocks(tree, source_lines, file_path)
        )

        # Stable ordering: file → line
        result.smells.sort(key=lambda s: (s.file_path, s.line_start))
        return result

    # ------------------------------------------------------------------
    # Individual detectors
    # ------------------------------------------------------------------

    def detect_long_methods(
        self, tree: ast.AST, file_path: str = "<string>"
    ) -> List[CodeSmell]:
        """Return smells for every function/method that exceeds *long_method_threshold* lines."""
        smells: List[CodeSmell] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            n_lines = _count_lines(node)
            if n_lines >= self.long_method_threshold:
                severity = (
                    "high" if n_lines >= self.long_method_threshold * 2
                    else "medium" if n_lines >= int(self.long_method_threshold * 1.5)
                    else "low"
                )
                smells.append(
                    CodeSmell(
                        smell_type="long_method",
                        file_path=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno,  # type: ignore[attr-defined]
                        name=node.name,
                        description=(
                            f"Function '{node.name}' is {n_lines} lines "
                            f"(threshold: {self.long_method_threshold})"
                        ),
                        severity=severity,
                    )
                )
        return smells

    def detect_deep_nesting(
        self, tree: ast.AST, file_path: str = "<string>"
    ) -> List[CodeSmell]:
        """Return smells for functions/methods whose control-flow nesting exceeds the threshold."""
        smells: List[CodeSmell] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            depth = _max_nesting_depth(node)
            if depth >= self.nesting_threshold:
                severity = (
                    "high" if depth >= self.nesting_threshold + 2
                    else "medium" if depth >= self.nesting_threshold + 1
                    else "low"
                )
                smells.append(
                    CodeSmell(
                        smell_type="deep_nesting",
                        file_path=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno,  # type: ignore[attr-defined]
                        name=node.name,
                        description=(
                            f"Function '{node.name}' has nesting depth {depth} "
                            f"(threshold: {self.nesting_threshold})"
                        ),
                        severity=severity,
                    )
                )
        return smells

    def detect_duplicate_blocks(
        self,
        tree: ast.AST,
        source_lines: List[str],
        file_path: str = "<string>",
    ) -> List[CodeSmell]:
        """Return smells for repeated statement blocks within the same module.

        A "block" is a sequence of consecutive top-level-or-function-body statements
        whose combined line span is at least *duplicate_block_min_lines*.  Two blocks
        are considered duplicates when their source text hashes match.
        """
        smells: List[CodeSmell] = []
        # Collect all statement sequences (body of each scope).
        all_bodies: List[List[ast.stmt]] = []
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                all_bodies.append(body)

        # Build a mapping: hash -> list of (line_start, line_end) occurrences.
        seen: dict[str, List[tuple[int, int]]] = {}
        min_stmts = 2  # Minimum consecutive statements to form a candidate block.

        for body in all_bodies:
            for window_size in range(min_stmts, len(body) + 1):
                for start_idx in range(len(body) - window_size + 1):
                    window = body[start_idx : start_idx + window_size]
                    try:
                        first = window[0]
                        last = window[-1]
                        span = last.end_lineno - first.lineno + 1  # type: ignore[attr-defined]
                    except AttributeError:
                        continue
                    if span < self.duplicate_block_min_lines:
                        continue
                    # Hash is the concatenation of individual statement hashes.
                    combined = "|".join(_stmt_hash(s, source_lines) for s in window)
                    block_hash = hashlib.md5(combined.encode()).hexdigest()  # noqa: S324
                    key = f"{block_hash}:{window_size}"
                    seen.setdefault(key, []).append(
                        (first.lineno, last.end_lineno)  # type: ignore[attr-defined]
                    )

        # Emit a smell for each hash that appears in more than one location.
        reported: set[tuple[int, int]] = set()  # avoid double-reporting
        for locations in seen.values():
            if len(locations) < 2:
                continue
            # Deduplicate overlapping ranges that were already reported.
            for line_start, line_end in locations:
                if (line_start, line_end) in reported:
                    continue
                reported.add((line_start, line_end))
                span = line_end - line_start + 1
                smells.append(
                    CodeSmell(
                        smell_type="duplicate_block",
                        file_path=file_path,
                        line_start=line_start,
                        line_end=line_end,
                        name=None,
                        description=(
                            f"Duplicate code block ({span} lines) — appears in "
                            f"{len(locations)} location(s)"
                        ),
                        severity="medium",
                    )
                )
        return smells
