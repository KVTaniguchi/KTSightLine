"""`xcodebuild` invocation.

The other half of ADR-0003's Xcode story. Vendored rather than depending on an MCP
server (D4): the caching key and the failure semantics below are policy, and an upstream
wrapper has no reason to implement them our way.

Two things here are load-bearing rather than convenience:

* **The DerivedData cache key.** ADR-0003 §3 calls the base-branch build the single
  largest avoidable cost, and warns that a subtly wrong key is worse than no cache —
  we would compare against the wrong baseline and produce confident false positives,
  the worst failure mode in the system. The key covers everything that changes the
  build product, and a miss is a clean rebuild rather than a partial.
* **A failed head build is a finding, not an error.** ADR-0003 §7: it is the most
  useful comment we could make, and the build log is its evidence.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess]

DEFAULT_TIMEOUT_S = 1800  # ADR-0003: the job timeout is 30 min; a single build is under it


class XcodeBuildError(RuntimeError):
    """The invocation itself could not run. A *compile* failure is not this."""


@dataclass(frozen=True)
class Destination:
    """A `-destination` specifier. Prefer udid: names are ambiguous across runtimes."""

    udid: str | None = None
    name: str | None = None
    platform: str = "iOS Simulator"

    def as_arg(self) -> str:
        if self.udid:
            return f"id={self.udid}"
        if self.name:
            return f"platform={self.platform},name={self.name}"
        raise XcodeBuildError("destination needs a udid or a name")


@dataclass(frozen=True)
class BuildResult:
    succeeded: bool
    returncode: int
    log: str
    command: tuple[str, ...]
    result_bundle: Path | None = None

    @property
    def failure_lines(self) -> tuple[str, ...]:
        """Compiler error lines, for the build-failed finding's claim."""
        return tuple(
            line.strip()
            for line in self.log.splitlines()
            if ": error:" in line or line.startswith("error:")
        )

    @property
    def warning_lines(self) -> tuple[str, ...]:
        return tuple(line.strip() for line in self.log.splitlines() if ": warning:" in line)


def _default_runner(cmd: Sequence[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)


def derived_data_key(
    *,
    commit_sha: str,
    project: Path,
    extra: Sequence[Path] = (),
) -> str:
    """Cache key for a branch's build products.

    Covers the commit, the project file, and any resolved-dependency manifests. A file
    that is missing contributes its absence rather than being skipped silently — a key
    that ignores a deleted `Package.resolved` would collide with one that had it.
    """
    h = hashlib.sha256()
    h.update(commit_sha.encode())
    for path in [Path(project), *(Path(p) for p in extra)]:
        h.update(b"\0" + str(path).encode() + b"\0")
        try:
            h.update(hashlib.sha256(Path(path).read_bytes()).digest())
        except (OSError, IsADirectoryError):
            if Path(path).is_dir():
                pbx = Path(path) / "project.pbxproj"
                h.update(
                    hashlib.sha256(pbx.read_bytes()).digest() if pbx.exists() else b"<missing>"
                )
            else:
                h.update(b"<missing>")
    return h.hexdigest()[:32]


@dataclass
class XcodeBuild:
    project: Path
    scheme: str
    derived_data: Path
    run: Runner = _default_runner
    timeout_s: int = DEFAULT_TIMEOUT_S
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    def _base(self, action: str, destination: Destination) -> list[str]:
        return [
            "xcodebuild", action,
            "-project", str(self.project),
            "-scheme", self.scheme,
            "-destination", destination.as_arg(),
            "-derivedDataPath", str(self.derived_data),
            *self.extra_args,
        ]  # fmt: skip

    def _invoke(self, cmd: list[str], *, result_bundle: Path | None = None) -> BuildResult:
        try:
            proc = self.run(cmd, self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise XcodeBuildError(
                f"xcodebuild exceeded {self.timeout_s}s. ADR-0003 requires this to fail "
                "open: report neutral, never block the merge."
            ) from exc
        log = (proc.stdout or "") + (proc.stderr or "")
        return BuildResult(
            succeeded=proc.returncode == 0,
            returncode=proc.returncode,
            log=log,
            command=tuple(cmd),
            result_bundle=result_bundle,
        )

    def build(self, destination: Destination) -> BuildResult:
        return self._invoke(self._base("build", destination))

    def build_for_testing(self, destination: Destination) -> BuildResult:
        """Split from `test` so a base-branch build can be cached without running tests."""
        return self._invoke(self._base("build-for-testing", destination))

    def test(
        self,
        destination: Destination,
        *,
        result_bundle: Path,
        only_testing: Sequence[str] = (),
    ) -> BuildResult:
        cmd = self._base("test", destination)
        cmd += ["-resultBundlePath", str(result_bundle)]
        for identifier in only_testing:
            cmd += [f"-only-testing:{identifier}"]
        if Path(result_bundle).exists():
            raise XcodeBuildError(
                f"{result_bundle} already exists; xcodebuild refuses to overwrite a "
                "result bundle. Use a fresh path per run."
            )
        return self._invoke(cmd, result_bundle=Path(result_bundle))

    def list_schemes(self) -> tuple[str, ...]:
        proc = self.run(["xcodebuild", "-list", "-project", str(self.project)], self.timeout_s)
        if proc.returncode != 0:
            raise XcodeBuildError(f"xcodebuild -list failed: {(proc.stderr or '').strip()}")
        out, schemes, in_section = proc.stdout or "", [], False
        for raw in out.splitlines():
            line = raw.strip()
            if line == "Schemes:":
                in_section = True
                continue
            if in_section:
                if not line:
                    break
                schemes.append(line)
        return tuple(schemes)
