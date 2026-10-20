"""The acceptance driver Sightline injects: a fixed interpreter plus one encoded payload.

ADR-0004 §4. Criteria text is often written by a model, and it travels into Swift. So
none of it is ever spliced into Swift source: the whole criteria set is one JSON
document, base64-encoded into a single string literal. Base64's alphabet cannot close a
string literal or open a ``\\(`` interpolation, which removes the escaping problem as a
class instead of testing for it string by string.

The Swift below is the interpreter. It never asserts — XCTest's pass/fail is not the
verdict. It writes records the harness reads back from the ``.xcresult``:

    SIGHTLINE-ACC|<base64url(JSON)>

with phases ``launched``, ``alert``, ``surface``, ``step``, ``expect``, ``shot`` and
``done``. Observed values are app text and can contain anything, so they travel encoded
too; the parser never splits app text on a delimiter.

API names here were checked against the Xcode 27.1 ``XCUIAutomation`` headers
(verification-2026-10-11 V5): ``waitForNonExistence(timeout:)``, ``launchEnvironment``,
``XCUIElementQuery.count``, ``XCUIElementAttributes.value`` / ``isEnabled``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from sightline.config import PermissionAnswer, RepoConfig
from sightline.core.acceptance.criteria import CriteriaFile, Criterion

DRIVER_FILENAME = "SightlineAcceptance.swift"
DRIVER_CLASS = "SightlineAcceptance"
PAYLOAD_VERSION = 1
RECORD_PREFIX = "SIGHTLINE-ACC|"
SHOT_PREFIX = "SIGHTLINE-ACC-SHOT-"
MAX_SWIPES = 10


@dataclass(frozen=True)
class RenderedDriver:
    source: str
    method_names: tuple[str, ...]
    criterion_ids: tuple[str, ...]
    """Index ``i`` in records is ``criterion_ids[i]``."""


def _spec(criterion: Criterion, config: RepoConfig) -> dict[str, Any]:
    acceptance = config.acceptance
    args = ["-AppleLanguages", f"({acceptance.language})", "-AppleLocale", acceptance.locale]
    env: dict[str, str] = {}
    navigation: list[str] = []
    wait_for: str | None = None
    if criterion.surface:
        surface = config.surfaces[criterion.surface]
        args += surface.launch_arguments
        navigation = list(surface.taps)
        wait_for = surface.wait_for
    if criterion.fixture:
        fixture = config.fixtures[criterion.fixture]
        args += fixture.launch_arguments
        env.update(fixture.launch_environment)
    return {
        "id": criterion.id,
        "launchArguments": args,
        "launchEnvironment": env,
        "navigation": navigation,
        "waitFor": wait_for,
        "steps": [
            {"action": s.action, "target": s.target, "text": s.text} for s in criterion.steps
        ],
        "expect": [
            {
                "id": o.id,
                "exists": o.exists,
                "label": o.label,
                "labelContains": o.label_contains,
                "value": o.value,
                "enabled": o.enabled,
            }
            for o in criterion.expect
        ],
    }


def build_payload(
    criteria: list[Criterion],
    config: RepoConfig,
    *,
    dismiss_alerts: tuple[str, ...] = (),
    permission_answer: PermissionAnswer = "allow",
) -> dict[str, Any]:
    """The interpreter's whole input. Linux-testable; Swift only decodes it."""
    return {
        "v": PAYLOAD_VERSION,
        "timeout": config.acceptance.timeout_s,
        "maxSwipes": MAX_SWIPES,
        "alertButtons": list(dismiss_alerts or config.alert_titles(answer=permission_answer)),
        "criteria": [_spec(c, config) for c in criteria],
    }


def encode_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_record(encoded: str) -> dict[str, Any]:
    """Inverse of the Swift ``record`` helper: unpadded base64url JSON."""
    padded = encoded + "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def render_acceptance_driver(
    criteria: CriteriaFile | list[Criterion],
    config: RepoConfig,
    *,
    dismiss_alerts: tuple[str, ...] = (),
) -> RenderedDriver:
    """One test method per executable criterion, so ``-only-testing:`` still selects one.

    Manual criteria and evidence types with no executor get no method: there is nothing
    to run, and their verdict is decided without a simulator.
    """
    items = criteria.criteria if isinstance(criteria, CriteriaFile) else criteria
    runnable = [c for c in items if c.executable]
    payload = encode_payload(build_payload(runnable, config, dismiss_alerts=dismiss_alerts))
    methods = "\n\n".join(
        f"    func {c.method_name}() {{ run({i}) }}" for i, c in enumerate(runnable)
    )
    source = _TEMPLATE.replace("__CLASS__", DRIVER_CLASS).replace("__PAYLOAD__", payload)
    source = source.replace("__METHODS__", methods)
    return RenderedDriver(
        source=source,
        method_names=tuple(c.method_name for c in runnable),
        criterion_ids=tuple(c.id for c in runnable),
    )


# The interpreter. Plain string with placeholders rather than an f-string, so Swift's
# braces and `\(` need no doubling and nothing from the criteria can reach it except the
# base64 payload.
_TEMPLATE = r"""// Generated by Sightline. Do not edit; edits are overwritten on every run.
//
// A fixed interpreter for acceptance criteria (docs/adr/0004-acceptance-tier.md).
// The criteria travel as one base64 JSON payload; no criterion text is Swift source.
// This file never asserts: the harness reads the SIGHTLINE-ACC records, not pass/fail.
//
// Injected into a scratch clone of the repository, never into the working tree it was
// copied from.

import XCTest

@MainActor
final class __CLASS__: XCTestCase {

    private struct Payload: Decodable {
        let v: Int
        let timeout: Double
        let maxSwipes: Int
        let alertButtons: [String]
        let criteria: [Spec]
    }

    private struct Spec: Decodable {
        let id: String
        let launchArguments: [String]
        let launchEnvironment: [String: String]
        let navigation: [String]
        let waitFor: String?
        let steps: [Step]
        let expect: [Expectation]
    }

    private struct Step: Decodable {
        let action: String
        let target: String
        let text: String?
    }

    private struct Expectation: Decodable {
        let id: String
        let exists: Bool?
        let label: String?
        let labelContains: String?
        let value: String?
        let enabled: Bool?
    }

    private static let encoded = "__PAYLOAD__"

    private static let payload: Payload = {
        guard let data = Data(base64Encoded: encoded) else {
            fatalError("Sightline payload is not base64; regenerate the driver")
        }
        do {
            return try JSONDecoder().decode(Payload.self, from: data)
        } catch {
            fatalError("Sightline payload does not decode: \(error)")
        }
    }()

    override func setUp() {
        continueAfterFailure = true
    }

    // MARK: records

    private func record(_ index: Int, _ phase: String, _ fields: [String: Any] = [:]) {
        var body = fields
        body["v"] = 1
        body["i"] = index
        body["id"] = Self.payload.criteria[index].id
        body["phase"] = phase
        guard let data = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys]) else {
            return
        }
        let encoded = data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        XCTContext.runActivity(named: "SIGHTLINE-ACC|" + encoded) { _ in }
    }

    private static func json(_ value: Any?) -> Any {
        guard let value else { return NSNull() }
        if let string = value as? String { return string }
        if let number = value as? NSNumber { return number }
        return "\(value)"
    }

    // MARK: element resolution - never firstMatch to act

    private func query(_ app: XCUIApplication, _ identifier: String) -> XCUIElementQuery {
        app.descendants(matching: .any).matching(identifier: identifier)
    }

    /// "ok", "unreachable", or "ambiguous". Waits, then scrolls a bounded number of times.
    ///
    /// More than one match is "ambiguous", never a guess: `firstMatch` would quietly pick
    /// one of three rows that share an identifier.
    private func tap(_ app: XCUIApplication, _ identifier: String) -> (String, Int) {
        let matches = query(app, identifier)
        _ = matches.firstMatch.waitForExistence(timeout: Self.payload.timeout)
        for _ in 0...Self.payload.maxSwipes {
            let count = matches.count
            if count > 1 { return ("ambiguous", count) }
            if count == 1 {
                let element = matches.element
                if element.isHittable {
                    element.tap()
                    return ("ok", 1)
                }
            }
            app.swipeUp()
        }
        return ("unreachable", matches.count)
    }

    /// Waiting acts on nothing, so any number of matches means the element appeared.
    private func appear(_ app: XCUIApplication, _ identifier: String) -> (String, Int) {
        let matches = query(app, identifier)
        let appeared = matches.firstMatch.waitForExistence(timeout: Self.payload.timeout)
        return (appeared ? "ok" : "unreachable", matches.count)
    }

    private func typeInto(_ app: XCUIApplication, _ identifier: String, _ text: String) -> (String, Int) {
        let (status, count) = tap(app, identifier)
        if status == "ok" {
            query(app, identifier).element.typeText(text)
        }
        return (status, count)
    }

    /// Taps through system alerts covering the app, using only configured titles (D14).
    private func dismissSystemAlerts(_ index: Int) {
        guard !Self.payload.alertButtons.isEmpty else { return }
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for _ in 0..<3 {
            let alert = springboard.alerts.firstMatch
            guard alert.waitForExistence(timeout: 3) else { return }
            guard let button = Self.payload.alertButtons.lazy
                .map({ alert.buttons[$0] })
                .first(where: { $0.exists })
            else {
                record(index, "alert", ["status": "undismissed", "label": alert.label])
                return
            }
            button.tap()
        }
    }

    // MARK: observation

    /// Waits, bounded, for an element matching every expected attribute, then records
    /// what is actually there. The predicate's arguments are values, never format text.
    private func observe(_ index: Int, _ n: Int, _ app: XCUIApplication, _ e: Expectation) {
        let timeout = Self.payload.timeout
        let matches = query(app, e.id)
        if e.exists == false {
            let gone = matches.firstMatch.waitForNonExistence(timeout: timeout)
            record(index, "expect", ["n": n, "status": gone ? "absent" : "observed",
                                     "count": matches.count, "actual": ["exists": !gone]])
            return
        }

        var format = ["identifier == %@"]
        var arguments: [Any] = [e.id]
        if let label = e.label { format.append("label == %@"); arguments.append(label) }
        if let part = e.labelContains { format.append("label CONTAINS %@"); arguments.append(part) }
        if let value = e.value { format.append("value == %@"); arguments.append(value) }
        if let enabled = e.enabled { format.append("enabled == %@"); arguments.append(NSNumber(value: enabled)) }
        let predicate = NSPredicate(format: format.joined(separator: " AND "), argumentArray: arguments)
        // Bounded: on timeout we record whatever is there, and that is the observation.
        _ = app.descendants(matching: .any).matching(predicate).firstMatch.waitForExistence(timeout: timeout)

        let count = matches.count
        if count == 0 {
            record(index, "expect", ["n": n, "status": "absent", "count": 0, "actual": ["exists": false]])
            return
        }
        if count > 1 {
            record(index, "expect", ["n": n, "status": "ambiguous", "count": count])
            return
        }
        let element = matches.element
        let actual: [String: Any] = [
            "exists": element.exists,
            "label": element.label,
            "value": Self.json(element.value),
            "enabled": element.isEnabled,
        ]
        record(index, "expect", ["n": n, "status": "observed", "count": 1, "actual": actual])
    }

    // MARK: the interpreter

    private func run(_ index: Int) {
        let spec = Self.payload.criteria[index]
        let app = XCUIApplication()
        app.launchArguments = spec.launchArguments
        app.launchEnvironment = spec.launchEnvironment
        app.launch()
        record(index, "launched")
        dismissSystemAlerts(index)
        defer { record(index, "done") }

        for (n, target) in spec.navigation.enumerated() {
            let (status, count) = tap(app, target)
            if status != "ok" {
                record(index, "surface", ["status": status, "target": target, "n": n, "count": count])
                return
            }
        }
        if let waitFor = spec.waitFor {
            let (status, count) = appear(app, waitFor)
            if status != "ok" {
                record(index, "surface", ["status": status, "target": waitFor, "count": count])
                return
            }
        }
        record(index, "surface", ["status": "ok"])

        for (n, step) in spec.steps.enumerated() {
            let result: (String, Int)
            switch step.action {
            case "tap": result = tap(app, step.target)
            case "wait_for": result = appear(app, step.target)
            case "type": result = typeInto(app, step.target, step.text ?? "")
            default: result = ("unknown_action", 0)
            }
            record(index, "step", ["n": n, "status": result.0, "target": step.target, "count": result.1])
            if result.0 != "ok" { return }
        }

        for (n, expectation) in spec.expect.enumerated() {
            observe(index, n, app, expectation)
        }

        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "SIGHTLINE-ACC-SHOT-\(index)"
        shot.lifetime = .keepAlways
        add(shot)
        record(index, "shot", ["name": "SIGHTLINE-ACC-SHOT-\(index)"])
    }

__METHODS__
}
"""
