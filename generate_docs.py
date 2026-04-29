#!/usr/bin/env python3
"""
GitScribe AI — generate_docs.py
Scans a repository, builds context from key files, and generates a
professional README.md via the Groq API. Commits the result back to
the repo using the GitHub API.
"""

import os
import re
import sys
import logging
from pathlib import Path

from github import Github, GithubException
from groq import Groq

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gitscribe")

# File patterns that carry meaningful architectural context
SCAN_PATTERNS = [
    "**/*.tf",
    "**/*.yml",
    "**/*.yaml",
    "**/Dockerfile",
    "**/requirements.txt",
    "**/package.json",
    "**/main.py",
    "**/app.py",
    "**/index.js",
    "**/index.ts",
]

# Hard limits to stay within model context windows
MAX_FILE_BYTES = 8_000        # per file
MAX_TOTAL_CHARS = 60_000      # total context sent to the model
README_COMMIT_MSG = "docs: auto-generate README.md via GitScribe AI"

SYSTEM_PROMPT = (
    "You are an expert technical writer. "
    "Based on the repository code provided, generate a professional, "
    "human-readable README.md using GitHub-flavored Markdown. "
    "Structure it with the following sections:\n"
    "1. **Project Overview** — what the project does and why it exists.\n"
    "2. **Tech Stack** — list every technology, language, and tool detected, "
    "with a brief description of its role.\n"
    "3. **Architecture** — a Mermaid.js flowchart (`graph TD`) that visually "
    "represents the main components and data flow.\n"
    "4. **Installation & Usage** — step-by-step setup instructions "
    "(prerequisites, environment variables, commands to run).\n"
    "5. **Contributing** — a short contributing guide.\n"
    "6. **License** — placeholder MIT license line.\n\n"
    "Rules:\n"
    "- Output ONLY the raw Markdown. Do NOT wrap it in a code fence.\n"
    "- Use badges, tables, and icons where appropriate.\n"
    "- Keep the tone professional yet approachable.\n"
    "- Make the Mermaid diagram accurate to the files you were shown.\n"
)


# ---------------------------------------------------------------------------
# File scanning helpers
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    """Skip hidden directories and common noise folders."""
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".terraform"}
    return any(part in skip_dirs for part in path.parts)


def collect_files(root: Path) -> list[Path]:
    """Return a deduplicated, sorted list of candidate files."""
    seen: set[Path] = set()
    results: list[Path] = []

    for pattern in SCAN_PATTERNS:
        for match in sorted(root.glob(pattern)):
            resolved = match.resolve()
            if resolved not in seen and match.is_file() and not _should_skip(match):
                seen.add(resolved)
                results.append(match)

    return results


def read_file_content(path: Path) -> str:
    """Read a file, truncating if it exceeds MAX_FILE_BYTES."""
    try:
        raw = path.read_bytes()
        # Attempt UTF-8 first, fall back to latin-1 to avoid crashes on binary
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        if len(raw) > MAX_FILE_BYTES:
            text = text[:MAX_FILE_BYTES] + "\n... [truncated]"

        return text
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return ""


def build_context(root: Path) -> str:
    """Assemble a single context string from all discovered files."""
    files = collect_files(root)

    if not files:
        log.warning("No relevant files found under %s", root)
        return "# (no scannable files found)"

    log.info("Discovered %d file(s) to include in context.", len(files))

    chunks: list[str] = []
    total_chars = 0

    for fpath in files:
        content = read_file_content(fpath)
        if not content.strip():
            continue

        rel = fpath.relative_to(root)
        header = f"### File: {rel}\n```\n"
        footer = "\n```\n"
        block = header + content + footer

        if total_chars + len(block) > MAX_TOTAL_CHARS:
            log.warning(
                "Context limit reached. Skipping %s and any remaining files.", rel
            )
            break

        chunks.append(block)
        total_chars += len(block)
        log.info("  + %s (%d chars)", rel, len(content))

    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Groq generation
# ---------------------------------------------------------------------------

def generate_readme(context: str, model: str) -> str:
    """Call Groq and return the generated README content."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log.error("GROQ_API_KEY is not set.")
        sys.exit(1)

    client = Groq(api_key=api_key)

    user_message = (
        "Here is the repository content. Generate the README.md now.\n\n"
        + context
    )

    log.info("Sending context to Groq (model: %s) …", model)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.4,
            max_tokens=4096,
        )
    except Exception as exc:
        log.error("Groq API call failed: %s", exc)
        sys.exit(1)

    raw = response.choices[0].message.content
    readme = _strip_thinking(raw)
    log.info("README generated (%d chars).", len(readme))
    return readme


def _strip_thinking(text: str) -> str:
    """
    Remove chain-of-thought blocks that some models (e.g. Qwen3) emit.
    Handles both <think>…</think> XML tags and bare reasoning preambles
    that end before the first Markdown heading.
    """
    # Remove <think>…</think> blocks (Qwen3 / DeepSeek style)
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # If the model prefixed plain reasoning text before the first heading,
    # discard everything before the first `#` character.
    first_heading = cleaned.find("#")
    if first_heading > 0:
        cleaned = cleaned[first_heading:]

    return cleaned.strip()


# ---------------------------------------------------------------------------
# GitHub commit helper
# ---------------------------------------------------------------------------

def commit_readme(readme_content: str, repo_name: str, target_branch: str) -> None:
    """Commit README.md to the target branch via the GitHub API."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.error("GITHUB_TOKEN is not set.")
        sys.exit(1)

    gh = Github(token)

    try:
        repo = gh.get_repo(repo_name)
    except GithubException as exc:
        log.error("Could not access repository '%s': %s", repo_name, exc)
        sys.exit(1)

    readme_path = "README.md"
    encoded = readme_content.encode("utf-8")

    try:
        existing = repo.get_contents(readme_path, ref=target_branch)
        repo.update_file(
            path=readme_path,
            message=README_COMMIT_MSG,
            content=encoded,
            sha=existing.sha,
            branch=target_branch,
        )
        log.info("README.md updated on branch '%s'.", target_branch)
    except GithubException as exc:
        if exc.status == 404:
            repo.create_file(
                path=readme_path,
                message=README_COMMIT_MSG,
                content=encoded,
                branch=target_branch,
            )
            log.info("README.md created on branch '%s'.", target_branch)
        else:
            log.error("GitHub API error: %s", exc)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Inputs are injected as environment variables by action.yml
    repo_name    = os.environ.get("INPUT_REPO_NAME")     or os.environ.get("GITHUB_REPOSITORY", "")
    target_branch = os.environ.get("INPUT_BRANCH")       or os.environ.get("GITHUB_REF_NAME", "main")
    model         = os.environ.get("INPUT_MODEL",         "qwen/qwen3-32b")
    workspace     = os.environ.get("GITHUB_WORKSPACE",   ".")

    if not repo_name:
        log.error(
            "Repository name not found. Set INPUT_REPO_NAME or ensure "
            "GITHUB_REPOSITORY is available."
        )
        sys.exit(1)

    root = Path(workspace).resolve()
    log.info("GitScribe AI starting — repo: %s | branch: %s | model: %s",
             repo_name, target_branch, model)

    context = build_context(root)
    readme  = generate_readme(context, model)

    commit_readme(readme, repo_name, target_branch)
    log.info("Done. README.md committed successfully.")


if __name__ == "__main__":
    main()
