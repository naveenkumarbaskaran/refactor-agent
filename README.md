# refactor-agent

An AI-powered Python code smell detector and refactoring agent built on
[Claude](https://anthropic.com) (`claude-sonnet-4-6`) and the Anthropic Python SDK.

## Features

- **AST-based smell detection** — finds long methods, deep nesting, and duplicate blocks
  without making a single API call.
- **AI-driven refactoring** — Claude reads the affected files, reasons about the best
  fix, and proposes new code.
- **Diff-first workflow** — a unified diff is always printed before any file is written,
  so you see exactly what will change.
- **Dry-run mode** — preview proposed changes without touching disk.
- **Interactive confirmation** — prompted before each write; skip with `--yes`.
- **Rich CLI** — colour-coded tables, severity indicators, JSON output mode.

## Installation

```bash
pip install refactor-agent
# or, for development:
git clone https://github.com/example/refactor-agent
cd refactor-agent
pip install -e .
```

Requires Python 3.10+.

## Quick Start

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

# 1. Scan a directory for all smells (no API call, AST only):
refactor-agent scan ./src

# 2. Scan for a specific smell:
refactor-agent scan ./src --smell long-method
refactor-agent scan ./src --smell nesting
refactor-agent scan ./src --smell duplication

# 3. Preview AI-suggested refactors (dry-run):
refactor-agent apply ./src --dry-run

# 4. Apply refactors interactively (prompted before each write):
refactor-agent apply ./src

# 5. Apply all refactors without confirmation:
refactor-agent apply ./src --yes
```

## CLI Reference

### `scan`

```
refactor-agent scan [OPTIONS] PATH

Options:
  --smell [all|long-method|nesting|duplication]
                  Which smell type to detect. [default: all]
  --format [table|json|text]
                  Output format. [default: table]
  --long-method-threshold INTEGER
                  Lines threshold for long-method detection. [default: 20]
  --nesting-threshold INTEGER
                  Depth threshold for nesting detection. [default: 4]
```

### `apply`

```
refactor-agent apply [OPTIONS] PATH

Options:
  --smell [all|long-method|nesting|duplication]
                  Which smell type to refactor. [default: all]
  --dry-run       Show proposed changes as diffs without writing any files.
  -y, --yes       Apply all suggested changes without asking for confirmation.
  --model TEXT    Claude model to use. [default: claude-sonnet-4-6]
  --api-key TEXT  Anthropic API key (or set ANTHROPIC_API_KEY env var).
```

## Python API

```python
from refactor_agent import RefactorAgent, SmellDetector

# --- AST-only scanning (no API key needed) ---
detector = SmellDetector(long_method_threshold=20, nesting_threshold=4)
result = detector.analyse_file("my_module.py")
print(result.summary())

for smell in result.by_type("long_method"):
    print(smell)

# --- AI-powered refactoring ---
agent = RefactorAgent(
    dry_run=True,        # don't write files
    confirm_writes=True, # ask before each write (ignored in dry_run)
)

# Scan-then-refactor in one call:
response = agent.refactor("./src", smell_types=["long_method"])
print(response)

# Or run an arbitrary natural-language task:
response = agent.run(
    "Extract the database connection logic from db.py into a separate module."
)
print(response)

# Check which files were written (or would be written in dry-run):
print(agent.written_files)
```

## Detected Smells

| Smell | Description | Default threshold |
|---|---|---|
| `long_method` | Function/method body exceeds N lines | 20 lines |
| `deep_nesting` | Control-flow nesting depth exceeds N | 4 levels |
| `duplicate_block` | Identical statement sequences appear in multiple places | 5+ lines |

Severity is scaled relative to the threshold:
- **low** — slightly over the threshold
- **medium** — moderately over
- **high** — significantly over (≥ 2× threshold for long methods)

## How It Works

```
┌───────────────────────────────────────────────────────────┐
│  refactor-agent apply ./src                               │
│                                                           │
│  1. SmellDetector scans files with ast.walk()             │
│     → DetectionResult (list of CodeSmell objects)         │
│                                                           │
│  2. RefactorAgent sends detection report + task to Claude │
│     with three tools:                                     │
│       read_file(path)                                     │
│       write_file(path, content)                           │
│       list_files(directory, pattern)                      │
│                                                           │
│  3. Claude reasons about each smell, reads affected files │
│     and proposes refactored versions.                     │
│                                                           │
│  4. For each write_file call:                             │
│       a. A unified diff is printed                        │
│       b. User is prompted (unless --yes / dry_run)        │
│       c. File is written if confirmed                     │
│                                                           │
│  5. Claude summarises all changes made.                   │
└───────────────────────────────────────────────────────────┘
```

## Configuration

All thresholds can be overridden either through the CLI flags or by instantiating
`SmellDetector` directly:

```python
detector = SmellDetector(
    long_method_threshold=30,     # flag a function only if > 30 lines
    nesting_threshold=5,          # flag nesting only at depth > 5
    duplicate_block_min_lines=8,  # minimum lines in a candidate duplicate block
)
```

## Requirements

- Python 3.10+
- `anthropic >= 0.40.0`
- `click >= 8.1`
- `rich >= 13.0`
- `ANTHROPIC_API_KEY` environment variable (for `apply` command only)

## License

MIT
