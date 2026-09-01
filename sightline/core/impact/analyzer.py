"""Deterministic impact analysis: a diff in, a trigger set out.

This is the layer ADR-0001 §2 leans on hardest. Dispatch selects skills by intersecting
their declared `triggers` with what this module emits, so **the impact layer's precision
is the product's precision**: if `ui_surface_changed` over-fires, every runtime skill
over-fires with it and we pay macOS runner minutes for nothing.

Rules here are pure pattern matching over paths and *added* lines. No model, ever
(ADR-0001 design rule 1). Every rule is one entry in RULES with a fixture test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from sightline.core.diff.models import ChangeType, Diff
from sightline.core.diff.swift import SwiftOutline
from sightline.core.skills.frontmatter import Trigger


@dataclass(frozen=True)
class TriggerEvidence:
    """Why a trigger fired. Written to the trajectory so dispatch is auditable."""

    trigger: Trigger
    path: str
    line: int | None
    reason: str


@dataclass(frozen=True)
class ImpactReport:
    triggers: frozenset[Trigger]
    evidence: tuple[TriggerEvidence, ...]
    changed_symbols: frozenset[str]
    ui_surfaces: frozenset[str]
    """Swift type names that look like SwiftUI/UIKit screens the diff reaches."""

    def why(self, trigger: Trigger) -> tuple[TriggerEvidence, ...]:
        return tuple(e for e in self.evidence if e.trigger is trigger)


# --- content patterns, matched against ADDED lines only ------------------------------
# "Added only" is deliberate: a `try!` that was already there is not this PR's problem.

_SWIFTUI_VIEW = re.compile(r"\bstruct\s+(\w+)\s*:\s*(?:[\w\s,]*\b)?View\b")
_UIKIT_VC = re.compile(
    r"\b(?:class|struct)\s+(\w+)\s*:\s*(?:[\w\s,]*\b)?(?:UIViewController|UIView)\b"
)
_ANY_TYPE = re.compile(r"\b(?:struct|class|enum|actor|protocol|extension)\s+(\w+)")

_PERMISSION_APIS = {
    "AVCaptureDevice": "camera or microphone",
    "PHPhotoLibrary": "photo library",
    "UIImagePickerController": "camera or photo library",
    "CLLocationManager": "location",
    "CNContactStore": "contacts",
    "CBCentralManager": "Bluetooth",
    "ATTrackingManager": "tracking",
    "EKEventStore": "calendar or reminders",
    "SFSpeechRecognizer": "speech recognition",
    "HKHealthStore": "health",
    "UNUserNotificationCenter": "notifications",
}

_CONCURRENCY_SUPPRESSIONS = (
    "@unchecked Sendable",
    "nonisolated(unsafe)",
    "@preconcurrency import",
    "MainActor.assumeIsolated",
)

_NAV_MARKERS = ("NavigationStack", "NavigationLink", "NavigationSplitView", "navigationDestination")


def _is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(p.endswith(("Tests", "UITests")) for p in parts)


def analyze(diff: Diff, *, sources: dict[str, str] | None = None) -> ImpactReport:
    """Emit the trigger set for a diff.

    ``sources`` maps head-side path to file content, used for symbol resolution. A path
    missing from it degrades to line-free evidence rather than failing.
    """
    sources = sources or {}
    evidence: list[TriggerEvidence] = []
    symbols: set[str] = set()
    surfaces: set[str] = set()

    def emit(trigger: Trigger, path: str, line: int | None, reason: str) -> None:
        evidence.append(TriggerEvidence(trigger=trigger, path=path, line=line, reason=reason))

    for changed in diff.files:
        path = changed.path
        name = PurePosixPath(path).name
        suffix = PurePosixPath(path).suffix

        emit(Trigger.FILE_CHANGED, path, None, f"{changed.change_type} {path}")

        # --- path-shaped triggers ---
        if name == "Info.plist":
            emit(Trigger.INFO_PLIST_CHANGED, path, None, "Info.plist changed")
        if name == "PrivacyInfo.xcprivacy":
            emit(Trigger.PRIVACY_MANIFEST_CHANGED, path, None, "privacy manifest changed")
        if suffix == ".entitlements":
            emit(Trigger.ENTITLEMENTS_CHANGED, path, None, "entitlements changed")
        if suffix in {".strings", ".stringsdict", ".xcstrings"} or ".lproj/" in path:
            emit(Trigger.LOCALIZATION_CHANGED, path, None, "localization resource changed")
        if ".xcassets/" in path:
            emit(Trigger.ASSET_CATALOG_CHANGED, path, None, "asset catalog changed")
        if ".xcdatamodeld" in path or suffix == ".xcdatamodel":
            emit(Trigger.CORE_DATA_MODEL_CHANGED, path, None, "Core Data model changed")
        if name == "Package.resolved":
            emit(Trigger.PACKAGE_RESOLVED_CHANGED, path, None, "resolved dependencies changed")
        if name in {"project.pbxproj", "Package.swift"} or suffix == ".xcconfig":
            emit(Trigger.BUILD_SETTINGS_CHANGED, path, None, "build settings changed")
        if _is_test_path(path):
            emit(Trigger.TEST_TARGET_CHANGED, path, None, "test target source changed")

        if suffix in {".xib", ".storyboard"}:
            emit(Trigger.UI_SURFACE_CHANGED, path, None, "interface builder file changed")

        if suffix != ".swift" or changed.change_type is ChangeType.DELETED:
            continue

        # --- Swift content triggers, from added lines only ---
        outline = SwiftOutline(sources[path]) if path in sources else None
        # Test sources reference and even declare views constantly. Letting them mark a
        # UI surface boots simulators for a tests-only PR — the exact waste ADR-0003's
        # gate exists to prevent.
        is_test = _is_test_path(path)
        touched_ui = False

        for line_no, text in changed.added_text():
            symbol = outline.enclosing_symbol(line_no) if outline else None
            if symbol and symbol != "<file>":
                symbols.add(f"{path}:{symbol}")

            if (m := _SWIFTUI_VIEW.search(text)) and not is_test:
                surfaces.add(m.group(1))
                touched_ui = True
                trigger = (
                    Trigger.VIEW_ADDED
                    if changed.change_type is ChangeType.ADDED
                    else Trigger.VIEW_MODIFIED
                )
                emit(trigger, path, line_no, f"declares SwiftUI View {m.group(1)}")
            elif (m := _UIKIT_VC.search(text)) and not is_test:
                surfaces.add(m.group(1))
                touched_ui = True
                emit(Trigger.VIEW_MODIFIED, path, line_no, f"declares UIKit {m.group(1)}")

            if any(marker in text for marker in _NAV_MARKERS) and not is_test:
                touched_ui = True
                emit(Trigger.NAVIGATION_GRAPH_CHANGED, path, line_no, "navigation API touched")

            for api, what in _PERMISSION_APIS.items():
                if api in text:
                    emit(
                        Trigger.PERMISSION_API_REFERENCED,
                        path,
                        line_no,
                        f"{api} referenced ({what})",
                    )

            for suppression in _CONCURRENCY_SUPPRESSIONS:
                if suppression in text:
                    emit(
                        Trigger.CONCURRENCY_ANNOTATION_ADDED,
                        path,
                        line_no,
                        f"added {suppression}",
                    )

            if m := _ANY_TYPE.search(text):
                emit(Trigger.SWIFT_SYMBOL_CHANGED, path, line_no, f"declares {m.group(1)}")

        # A .swift file whose declared type is a View counts as a UI surface even when
        # the added lines are only inside the body.
        declares_surface = outline is not None and bool(
            _SWIFTUI_VIEW.search(sources[path]) or _UIKIT_VC.search(sources[path])
        )
        if not touched_ui and not is_test and declares_surface:
            touched_ui = True
            for m in _SWIFTUI_VIEW.finditer(sources[path]):
                surfaces.add(m.group(1))
            emit(Trigger.VIEW_MODIFIED, path, None, "body of an existing View changed")

        if touched_ui:
            emit(Trigger.UI_SURFACE_CHANGED, path, None, "file declares a rendered surface")

    return ImpactReport(
        triggers=frozenset(e.trigger for e in evidence),
        evidence=tuple(evidence),
        changed_symbols=frozenset(symbols),
        ui_surfaces=frozenset(surfaces),
    )
