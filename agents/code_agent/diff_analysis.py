"""Turn a set of changed files into the structured signals an investigation reasons on.

**Owner:** P3. The Code agent's job is to say whether a recent change explains a build
failure. An LLM handed a raw diff will find a story in it either way; handed a
classified set of signals - "a dependency pin moved", "a config value changed type" -
it has something checkable to argue from, and the agent can say "no code evidence"
when there is none.

Nothing here calls an LLM or the network. It is pure functions over ``FileChange``
values, which is what makes the Week 3 scenarios testable without either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Iterable

from agents.code_agent.tools.github_client import FileChange


class FileRole(str, Enum):
    """What a changed file is for. Drives which hypotheses are even plausible."""

    DEPENDENCY_MANIFEST = "dependency_manifest"
    CONFIGURATION = "configuration"
    CI_PIPELINE = "ci_pipeline"
    DEPLOYMENT = "deployment"
    TEST = "test"
    SOURCE = "source"
    DOCUMENTATION = "documentation"
    OTHER = "other"


DEPENDENCY_MANIFEST_NAMES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "constraints.txt",
        "pyproject.toml",
        "poetry.lock",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
    }
)

CONFIGURATION_SUFFIXES = (".yaml", ".yml", ".ini", ".cfg", ".toml", ".conf", ".properties", ".env")
DOCUMENTATION_SUFFIXES = (".md", ".rst", ".txt")

CI_PATH_RE = re.compile(r"(^|/)(Jenkinsfile|\.github/workflows/|\.gitlab-ci\.yml|azure-pipelines\.yml)")
# Deployment means the machinery that ships the app. A `.env` file is read by the
# app at runtime, so it classifies as configuration (and is what TC-08 breaks).
DEPLOYMENT_PATH_RE = re.compile(r"(^|/)(deploy|k8s|kubernetes|helm|charts)/|(^|/)deploy\.sh$")
TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$|\.test\.[jt]sx?$")

#: `name==1.2.3`, `name>=1.2`, `"name": "^1.2.3"`. Captures the name and the specifier.
PINNED_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<operator>==|>=|<=|~=|>|<)\s*(?P<version>[0-9][^\s;#,\]\)]*)"
)
JSON_DEPENDENCY_RE = re.compile(
    r'^\s*"(?P<name>[@A-Za-z0-9][^"]*)"\s*:\s*"(?P<version>[^"]+)"'
)

#: `key: value` in YAML, `KEY=value` in env files, `key = value` in ini/toml.
CONFIG_ASSIGNMENT_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)\s*(?::|=)\s*(?P<value>.*?)\s*$"
)


def classify_path(path: str) -> FileRole:
    """Classify a repository path by what the file is for.

    Order matters: a `tests/` path wins over its `.py` suffix, and a CI definition
    wins over its `.yml` suffix, because that is the question being asked of it.
    """
    if not path:
        return FileRole.OTHER
    name = path.rsplit("/", 1)[-1]

    if CI_PATH_RE.search(path) or name == "Jenkinsfile":
        return FileRole.CI_PIPELINE
    if name in DEPENDENCY_MANIFEST_NAMES:
        return FileRole.DEPENDENCY_MANIFEST
    if TEST_PATH_RE.search(path):
        return FileRole.TEST
    if DEPLOYMENT_PATH_RE.search(path) or name == "Dockerfile":
        return FileRole.DEPLOYMENT
    if name.startswith(".env") or name.endswith(CONFIGURATION_SUFFIXES):
        return FileRole.CONFIGURATION
    if name.endswith(DOCUMENTATION_SUFFIXES):
        return FileRole.DOCUMENTATION
    if name.endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".sh")):
        return FileRole.SOURCE
    return FileRole.OTHER


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One added or removed line, kept with the file it came from."""

    path: str
    text: str
    added: bool

    @property
    def content(self) -> str:
        """The line without its diff marker."""
        return self.text[1:] if self.text[:1] in {"+", "-"} else self.text


@dataclass(frozen=True, slots=True)
class DependencyChange:
    """A dependency whose pinned version moved, was added, or was dropped."""

    name: str
    path: str
    previous_version: str | None
    new_version: str | None

    @property
    def is_upgrade_or_downgrade(self) -> bool:
        return bool(self.previous_version and self.new_version)

    def describe(self) -> str:
        if self.previous_version and self.new_version:
            return f"{self.name} {self.previous_version} -> {self.new_version} in {self.path}"
        if self.new_version:
            return f"{self.name} {self.new_version} added in {self.path}"
        return f"{self.name} {self.previous_version} removed from {self.path}"


@dataclass(frozen=True, slots=True)
class ConfigurationChange:
    """A configuration key whose value changed, was added, or was removed."""

    key: str
    path: str
    previous_value: str | None
    new_value: str | None

    def describe(self) -> str:
        if self.previous_value is not None and self.new_value is not None:
            return f"{self.key}: {self.previous_value!r} -> {self.new_value!r} in {self.path}"
        if self.new_value is not None:
            return f"{self.key} set to {self.new_value!r} in {self.path}"
        return f"{self.key} removed from {self.path} (was {self.previous_value!r})"


@dataclass(frozen=True, slots=True)
class DiffAnalysis:
    """Everything the agent can say about a change set without asking an LLM."""

    files: tuple[FileChange, ...] = ()
    roles: dict[str, FileRole] = field(default_factory=dict)
    dependency_changes: tuple[DependencyChange, ...] = ()
    configuration_changes: tuple[ConfigurationChange, ...] = ()
    added_lines: tuple[DiffLine, ...] = ()
    removed_lines: tuple[DiffLine, ...] = ()

    @property
    def is_empty(self) -> bool:
        """No files changed at all - the strongest evidence *against* a code cause."""
        return not self.files

    @property
    def total_changed_lines(self) -> int:
        return sum(file.changed_lines for file in self.files)

    def paths_with_role(self, role: FileRole) -> tuple[str, ...]:
        return tuple(path for path, value in self.roles.items() if value is role)

    @property
    def touches_only_documentation(self) -> bool:
        """A docs-only change cannot break a build, so it argues against a code cause."""
        return bool(self.files) and all(
            role is FileRole.DOCUMENTATION for role in self.roles.values()
        )

    def summarize(self) -> str:
        """A one-line description for a log line or an evidence summary."""
        if self.is_empty:
            return "no files changed"
        counts: dict[FileRole, int] = {}
        for role in self.roles.values():
            counts[role] = counts.get(role, 0) + 1
        parts = [f"{count} {role.value}" for role, count in sorted(counts.items(), key=lambda i: i[0].value)]
        return f"{len(self.files)} file(s), {self.total_changed_lines} line(s): " + ", ".join(parts)


def iter_diff_lines(file: FileChange) -> Iterable[DiffLine]:
    """Yield the added and removed lines of a patch, skipping diff headers.

    ``+++``/``---`` are file headers, not content; treating them as changed lines is
    the classic way to produce a confident, wrong finding.
    """
    for raw in (file.patch or "").splitlines():
        if raw.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if raw.startswith("+"):
            yield DiffLine(path=file.path, text=raw, added=True)
        elif raw.startswith("-"):
            yield DiffLine(path=file.path, text=raw, added=False)


def _parse_dependency(line: str) -> tuple[str, str] | None:
    """Return ``(name, version)`` for a dependency declaration, else ``None``."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = PINNED_REQUIREMENT_RE.match(stripped)
    if match:
        return match.group("name").lower(), f"{match.group('operator')}{match.group('version')}"
    match = JSON_DEPENDENCY_RE.match(stripped)
    if match and not match.group("name").endswith(":"):
        return match.group("name").lower(), match.group("version")
    return None


def extract_dependency_changes(files: Iterable[FileChange]) -> tuple[DependencyChange, ...]:
    """Pair removed and added dependency lines into version movements.

    A changed pin shows up as a removed line and an added line for the same package;
    matching them by name is what turns "two lines differ" into "httpx was upgraded".
    """
    changes: list[DependencyChange] = []
    for file in files:
        if classify_path(file.path) is not FileRole.DEPENDENCY_MANIFEST:
            continue
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for line in iter_diff_lines(file):
            parsed = _parse_dependency(line.content)
            if parsed is None:
                continue
            name, version = parsed
            (after if line.added else before)[name] = version

        for name in sorted(set(before) | set(after)):
            previous, new = before.get(name), after.get(name)
            if previous == new:
                continue
            changes.append(
                DependencyChange(name=name, path=file.path, previous_version=previous, new_version=new)
            )
    return tuple(changes)


def _parse_configuration(line: str) -> tuple[str, str] | None:
    """Return ``(key, value)`` for a configuration assignment, else ``None``."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    match = CONFIG_ASSIGNMENT_RE.match(stripped)
    if not match:
        return None
    value = match.group("value").split("#", 1)[0].strip().strip("\"'")
    return match.group("key"), value


def extract_configuration_changes(files: Iterable[FileChange]) -> tuple[ConfigurationChange, ...]:
    """Pair removed and added configuration lines into key-level value changes."""
    changes: list[ConfigurationChange] = []
    for file in files:
        role = classify_path(file.path)
        if role not in {FileRole.CONFIGURATION, FileRole.DEPLOYMENT}:
            continue
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for line in iter_diff_lines(file):
            parsed = _parse_configuration(line.content)
            if parsed is None:
                continue
            key, value = parsed
            (after if line.added else before)[key] = value

        for key in sorted(set(before) | set(after)):
            previous, new = before.get(key), after.get(key)
            if previous == new:
                continue
            changes.append(
                ConfigurationChange(key=key, path=file.path, previous_value=previous, new_value=new)
            )
    return tuple(changes)


def analyze_changes(files: Iterable[FileChange]) -> DiffAnalysis:
    """Classify a change set and extract the dependency and configuration signals."""
    file_tuple = tuple(files)
    added: list[DiffLine] = []
    removed: list[DiffLine] = []
    for file in file_tuple:
        for line in iter_diff_lines(file):
            (added if line.added else removed).append(line)

    return DiffAnalysis(
        files=file_tuple,
        roles={file.path: classify_path(file.path) for file in file_tuple},
        dependency_changes=extract_dependency_changes(file_tuple),
        configuration_changes=extract_configuration_changes(file_tuple),
        added_lines=tuple(added),
        removed_lines=tuple(removed),
    )


__all__ = [
    "ConfigurationChange",
    "DependencyChange",
    "DiffAnalysis",
    "DiffLine",
    "FileRole",
    "analyze_changes",
    "classify_path",
    "extract_configuration_changes",
    "extract_dependency_changes",
    "iter_diff_lines",
]
