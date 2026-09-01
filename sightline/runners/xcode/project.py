"""Reading and editing `.xcodeproj` project files.

ADR-0003 §5 requires an adapter around project-file manipulation, because we will get
it wrong on somebody's project and the failure has to be legible.

**Format note.** A `project.pbxproj` is an OpenStep plist. `xcodebuild` also accepts the
file written back as an **XML** plist (verified 2026-08-31), so we parse OpenStep in,
edit in Python, and write XML out with stdlib `plistlib`.

The OpenStep reader is hand-written rather than shelling out to `plutil`. `plutil` works
and was the first implementation, but it is macOS-only, which meant this module — the one
doing the riskiest surgery in the codebase — could not be tested on the Linux job that
runs on every PR. The format is small enough that a parser is cheaper than the coverage
hole.

Losing Xcode's formatting and comments would be unacceptable in a user's checkout. It is
fine here because every edit happens in a scratch clone (see `injection.py`) and the
original is never touched. That constraint is what buys us the simple implementation.
"""

from __future__ import annotations

import plistlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HEADER = "// !$*UTF8*$!"

UI_TEST_PRODUCT_TYPE = "com.apple.product-type.bundle.ui-testing"
UNIT_TEST_PRODUCT_TYPE = "com.apple.product-type.bundle.unit-test"
APP_PRODUCT_TYPE = "com.apple.product-type.application"


class ProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    object_id: str
    name: str
    product_type: str

    @property
    def is_ui_test(self) -> bool:
        return self.product_type == UI_TEST_PRODUCT_TYPE

    @property
    def is_app(self) -> bool:
        return self.product_type == APP_PRODUCT_TYPE


# --- OpenStep plist reading -----------------------------------------------------------

_WS_OR_COMMENT = re.compile(r"(?:\s+|/\*.*?\*/|//[^\n]*)+", re.DOTALL)
_BARE = re.compile(r"[A-Za-z0-9_$./:@~-]+")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


class _Reader:
    """Recursive-descent reader for the OpenStep plist subset Xcode emits.

    Values are dicts, arrays, quoted strings, and bare strings. Every scalar comes back
    as a string, which is how pbxproj treats them anyway — `objectVersion` is "56", not
    56, and writing it back as a string is what Xcode expects.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _skip(self) -> None:
        if match := _WS_OR_COMMENT.match(self.text, self.pos):
            self.pos = match.end()

    def _peek(self) -> str:
        self._skip()
        if self.pos >= len(self.text):
            raise ValueError("unexpected end of file")
        return self.text[self.pos]

    def parse(self) -> Any:
        value = self.value()
        self._skip()
        if self.pos != len(self.text):
            raise ValueError(f"trailing content at offset {self.pos}")
        return value

    def value(self) -> Any:
        char = self._peek()
        if char == "{":
            return self.dictionary()
        if char == "(":
            return self.array()
        if char == '"':
            return self.quoted()
        return self.bare()

    def dictionary(self) -> dict[str, Any]:
        self.pos += 1  # {
        out: dict[str, Any] = {}
        while self._peek() != "}":
            key = self.quoted() if self._peek() == '"' else self.bare()
            if self._peek() != "=":
                raise ValueError(f"expected '=' after key {key!r} at {self.pos}")
            self.pos += 1
            out[key] = self.value()
            if self._peek() == ";":
                self.pos += 1
        self.pos += 1  # }
        return out

    def array(self) -> list[Any]:
        self.pos += 1  # (
        out: list[Any] = []
        while self._peek() != ")":
            out.append(self.value())
            if self._peek() == ",":
                self.pos += 1
        self.pos += 1  # )
        return out

    def quoted(self) -> str:
        self.pos += 1  # opening quote
        chars: list[str] = []
        while True:
            if self.pos >= len(self.text):
                raise ValueError("unterminated string")
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 1
                nxt = self.text[self.pos]
                chars.append(_ESCAPES.get(nxt, nxt))
            elif char == '"':
                self.pos += 1
                return "".join(chars)
            else:
                chars.append(char)
            self.pos += 1

    def bare(self) -> str:
        self._skip()
        match = _BARE.match(self.text, self.pos)
        if not match:
            raise ValueError(f"unexpected character {self.text[self.pos]!r} at {self.pos}")
        self.pos = match.end()
        return match.group(0)


def parse_openstep(text: str) -> dict[str, Any]:
    """Parse an OpenStep plist (the `project.pbxproj` format) into plain Python."""
    value = _Reader(text).parse()
    if not isinstance(value, dict):
        # ValueError, not TypeError: this is malformed *input*, not a caller passing the
        # wrong type. `_load` catches ValueError and reports it as a ProjectError.
        raise ValueError("top-level value is not a dictionary")  # noqa: TRY004
    return value


def _new_id() -> str:
    """24 uppercase hex characters, the shape Xcode uses for object ids."""
    return uuid.uuid4().hex[:24].upper()


class PbxProject:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.pbxproj = (
            self.path / "project.pbxproj" if self.path.suffix == ".xcodeproj" else self.path
        )
        if not self.pbxproj.exists():
            raise ProjectError(f"{self.pbxproj} does not exist")
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        raw = self.pbxproj.read_bytes()
        if raw.lstrip().startswith((b"<?xml", b"bplist")):
            return plistlib.loads(raw)
        text = raw.decode("utf-8")
        body = text.split("\n", 1)[1] if text.startswith(HEADER) else text
        try:
            return parse_openstep(body)
        except ValueError as exc:
            raise ProjectError(
                f"could not parse {self.pbxproj}: {exc}. Generated projects "
                "(XcodeGen, Tuist) may need to be generated first."
            ) from exc

    def save(self) -> None:
        """Write back as an XML plist. Only ever call this on a scratch clone."""
        self.pbxproj.write_bytes(plistlib.dumps(self.data, fmt=plistlib.FMT_XML))

    # --- reads ---

    @property
    def objects(self) -> dict[str, Any]:
        return self.data["objects"]

    def targets(self) -> list[Target]:
        return [
            Target(oid, obj.get("name", ""), obj.get("productType", ""))
            for oid, obj in self.objects.items()
            if obj.get("isa") == "PBXNativeTarget"
        ]

    def ui_test_targets(self) -> list[Target]:
        return [t for t in self.targets() if t.is_ui_test]

    def app_targets(self) -> list[Target]:
        return [t for t in self.targets() if t.is_app]

    def target_named(self, name: str) -> Target | None:
        return next((t for t in self.targets() if t.name == name), None)

    # --- writes ---

    def _sources_phase(self, target: Target) -> str:
        for phase_id in self.objects[target.object_id].get("buildPhases", []):
            if self.objects.get(phase_id, {}).get("isa") == "PBXSourcesBuildPhase":
                return phase_id
        raise ProjectError(f"target {target.name!r} has no sources build phase")

    def _group_for(self, target: Target) -> str:
        """The group whose path matches the target name, else the project's main group."""
        for oid, obj in self.objects.items():
            if obj.get("isa") == "PBXGroup" and obj.get("path") == target.name:
                return oid
        root = self.data["rootObject"]
        return self.objects[root]["mainGroup"]

    def add_source_file(self, target: Target, filename: str) -> str:
        """Register ``filename`` as a compiled source of ``target``.

        The file is referenced relative to the target's group, which is where
        :func:`injection.prepare_workspace` writes it. Idempotent: adding the same
        filename twice returns the existing reference rather than duplicating it.
        """
        group_id = self._group_for(target)
        for child in self.objects[group_id].get("children", []):
            if self.objects.get(child, {}).get("path") == filename:
                return child

        file_ref = _new_id()
        build_file = _new_id()
        self.objects[file_ref] = {
            "isa": "PBXFileReference",
            "lastKnownFileType": "sourcecode.swift",
            "path": filename,
            "sourceTree": "<group>",
        }
        self.objects[build_file] = {"isa": "PBXBuildFile", "fileRef": file_ref}
        self.objects[group_id].setdefault("children", []).append(file_ref)
        self.objects[self._sources_phase(target)].setdefault("files", []).append(build_file)
        return file_ref

    def group_path(self, target: Target) -> str:
        """Directory (relative to the project) that the target's group maps to."""
        return self.objects[self._group_for(target)].get("path", "")

    # --- generation (ADR-0003 §5, path 2) ---

    def create_ui_test_target(
        self,
        name: str,
        app_target: Target,
        *,
        bundle_id: str,
        deployment_target: str = "17.0",
    ) -> Target:
        """Add a UI test target that hosts the injected driver.

        For repos that have no UI test target at all, which is most of them. Only ever
        called against a scratch clone.

        ``performAccessibilityAudit`` lives in `XCUIAutomation.framework`, which only
        test targets link, so there is no way to run it without a target like this one.
        """
        if existing := self.target_named(name):
            return existing

        product = _new_id()
        group = _new_id()
        sources = _new_id()
        frameworks = _new_id()
        debug, release, config_list = _new_id(), _new_id(), _new_id()
        proxy, dependency, target_id = _new_id(), _new_id(), _new_id()

        settings = {
            "CODE_SIGNING_ALLOWED": "NO",
            "CODE_SIGN_IDENTITY": "",
            "CURRENT_PROJECT_VERSION": "1",
            "GENERATE_INFOPLIST_FILE": "YES",
            "IPHONEOS_DEPLOYMENT_TARGET": deployment_target,
            "MARKETING_VERSION": "1.0",
            "PRODUCT_BUNDLE_IDENTIFIER": bundle_id,
            "PRODUCT_NAME": "$(TARGET_NAME)",
            "SWIFT_VERSION": "5.0",
            "TARGETED_DEVICE_FAMILY": "1,2",
            "TEST_TARGET_NAME": app_target.name,
        }

        self.objects[product] = {
            "isa": "PBXFileReference",
            "explicitFileType": "wrapper.cfbundle",
            "includeInIndex": "0",
            "path": f"{name}.xctest",
            "sourceTree": "BUILT_PRODUCTS_DIR",
        }
        self.objects[group] = {
            "isa": "PBXGroup",
            "children": [],
            "path": name,
            "sourceTree": "<group>",
        }
        for phase_id, isa in (
            (sources, "PBXSourcesBuildPhase"),
            (frameworks, "PBXFrameworksBuildPhase"),
        ):
            self.objects[phase_id] = {
                "isa": isa,
                "buildActionMask": "2147483647",
                "files": [],
                "runOnlyForDeploymentPostprocessing": "0",
            }
        for cfg_id, cfg_name in ((debug, "Debug"), (release, "Release")):
            self.objects[cfg_id] = {
                "isa": "XCBuildConfiguration",
                "buildSettings": dict(settings),
                "name": cfg_name,
            }
        self.objects[config_list] = {
            "isa": "XCConfigurationList",
            "buildConfigurations": [debug, release],
            "defaultConfigurationIsVisible": "0",
            "defaultConfigurationName": "Release",
        }
        root = self.data["rootObject"]
        self.objects[proxy] = {
            "isa": "PBXContainerItemProxy",
            "containerPortal": root,
            "proxyType": "1",
            "remoteGlobalIDString": app_target.object_id,
            "remoteInfo": app_target.name,
        }
        self.objects[dependency] = {
            "isa": "PBXTargetDependency",
            "target": app_target.object_id,
            "targetProxy": proxy,
        }
        self.objects[target_id] = {
            "isa": "PBXNativeTarget",
            "buildConfigurationList": config_list,
            "buildPhases": [sources, frameworks],
            "buildRules": [],
            "dependencies": [dependency],
            "name": name,
            "productName": name,
            "productReference": product,
            "productType": UI_TEST_PRODUCT_TYPE,
        }

        project = self.objects[root]
        project.setdefault("targets", []).append(target_id)
        self.objects[project["mainGroup"]].setdefault("children", []).append(group)
        if product_group := project.get("productRefGroup"):
            self.objects[product_group].setdefault("children", []).append(product)
        attributes = project.setdefault("attributes", {}).setdefault("TargetAttributes", {})
        attributes[target_id] = {"TestTargetID": app_target.object_id}

        return Target(target_id, name, UI_TEST_PRODUCT_TYPE)


SCHEME_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion = "2660" version = "1.7">
   <BuildAction parallelizeBuildables = "YES" buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry buildForTesting = "YES" buildForRunning = "YES" \
buildForProfiling = "YES" buildForArchiving = "YES" buildForAnalyzing = "YES">
            <BuildableReference BuildableIdentifier = "primary" \
BlueprintIdentifier = "{app_id}" BuildableName = "{app_name}.app" \
BlueprintName = "{app_name}" ReferencedContainer = "container:{project_name}">
            </BuildableReference>
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction buildConfiguration = "Debug" shouldUseLaunchSchemeArgsEnv = "YES">
      <Testables>
         <TestableReference skipped = "NO">
            <BuildableReference BuildableIdentifier = "primary" \
BlueprintIdentifier = "{test_id}" BuildableName = "{test_name}.xctest" \
BlueprintName = "{test_name}" ReferencedContainer = "container:{project_name}">
            </BuildableReference>
         </TestableReference>
      </Testables>
   </TestAction>
   <LaunchAction buildConfiguration = "Debug" launchStyle = "0" \
useCustomWorkingDirectory = "NO" ignoresPersistentStateOnLaunch = "NO" \
debugDocumentVersioning = "YES" allowLocationSimulation = "YES">
      <BuildableProductRunnable runnableDebuggingMode = "0">
         <BuildableReference BuildableIdentifier = "primary" \
BlueprintIdentifier = "{app_id}" BuildableName = "{app_name}.app" \
BlueprintName = "{app_name}" ReferencedContainer = "container:{project_name}">
         </BuildableReference>
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction buildConfiguration = "Release"></ProfileAction>
   <AnalyzeAction buildConfiguration = "Debug"></AnalyzeAction>
   <ArchiveAction buildConfiguration = "Release"></ArchiveAction>
</Scheme>
"""


def write_scheme(project_path: Path, scheme_name: str, app: Target, ui_test: Target) -> Path:
    """Write a shared scheme whose test action runs ``ui_test``.

    Writing our own rather than editing the repo's: scheme XML varies a lot in the wild,
    and an empty ``<TestPlans>`` element silently overrides ``<Testables>`` — a failure
    that costs an afternoon to find. Generating one is smaller and more predictable.
    """
    project_path = Path(project_path)
    schemes = project_path / "xcshareddata" / "xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    destination = schemes / f"{scheme_name}.xcscheme"
    destination.write_text(
        SCHEME_TEMPLATE.format(
            app_id=app.object_id,
            app_name=app.name,
            test_id=ui_test.object_id,
            test_name=ui_test.name,
            project_name=project_path.name,
        )
    )
    return destination
