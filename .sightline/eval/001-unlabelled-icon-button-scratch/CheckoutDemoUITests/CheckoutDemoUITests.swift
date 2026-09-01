import XCTest

/// The UI test target Sightline discovers rather than generates (ADR-0003 §5, path 1).
///
/// Audit type spellings here are the exact `XCUIAccessibilityAuditType` members,
/// verified against the Xcode 26.6 headers on 2026-08-31: `textClipped` (not
/// `clippedText`), `trait` (not `traits`), `sufficientElementDescription` (not
/// `elementDescription`).
@MainActor
final class CheckoutDemoUITests: XCTestCase {

    private static let auditTypes: XCUIAccessibilityAuditType = [
        .contrast,
        .elementDetection,
        .hitRegion,
        .sufficientElementDescription,
        .dynamicType,
        .textClipped,
        .trait,
    ]

    override func setUp() {
        continueAfterFailure = true
    }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launch()
        return app
    }

    /// Scrolls until the control is hittable, then taps.
    ///
    /// Not defensive boilerplate: at accessibility-extra-extra-extra-large the list is
    /// tall enough that controls which are on-screen at default size are not. A driver
    /// that only taps works at default size and silently fails the entire AX5 matrix —
    /// which is the matrix the project exists to test.
    @discardableResult
    private func tap(_ app: XCUIApplication, _ identifier: String, file: StaticString = #filePath, line: UInt = #line) -> Bool {
        let element = app.descendants(matching: .any).matching(identifier: identifier).firstMatch
        for _ in 0..<10 {
            if element.exists && element.isHittable {
                element.tap()
                return true
            }
            app.swipeUp()
        }
        XCTFail("could not reach '\(identifier)' after scrolling", file: file, line: line)
        return false
    }

    /// Waits for an element by identifier regardless of its element type.
    private func awaitElement(_ app: XCUIApplication, _ identifier: String, timeout: TimeInterval = 10) -> Bool {
        app.descendants(matching: .any)
            .matching(identifier: identifier)
            .firstMatch
            .waitForExistence(timeout: timeout)
    }

    /// Maps the bitmask member back to its source spelling, so the .xcresult carries
    /// the name a skill author writes in frontmatter.
    private static func name(of type: XCUIAccessibilityAuditType) -> String {
        switch type {
        case .contrast: return "contrast"
        case .elementDetection: return "elementDetection"
        case .hitRegion: return "hitRegion"
        case .sufficientElementDescription: return "sufficientElementDescription"
        case .dynamicType: return "dynamicType"
        case .textClipped: return "textClipped"
        case .trait: return "trait"
        default: return "unknown(\(type.rawValue))"
        }
    }

    /// Records issues instead of failing, so one screen's defects don't hide the next
    /// screen's. The harness reads the .xcresult, not this test's pass/fail.
    ///
    /// The activity name is the harness's parse target, so it carries everything a
    /// Finding needs: audit type, element identity, and the frame.
    private func audit(_ app: XCUIApplication, screen: String) throws {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "SIGHTLINE-SCREENSHOT-\(screen)"
        shot.lifetime = .keepAlways
        add(shot)

        try app.performAccessibilityAudit(for: Self.auditTypes) { issue in
            let element = issue.element
            let identifier = element?.identifier ?? ""
            let label = element?.label ?? ""
            let frame = element.map { "\($0.frame)" } ?? "nil"
            let name = Self.name(of: issue.auditType)
            XCTContext.runActivity(
                named: "SIGHTLINE|\(screen)|\(name)|id=\(identifier)|label=\(label)|frame=\(frame)|\(issue.compactDescription)"
            ) { _ in }
            return true  // handled — do not fail the test
        }
    }

    func testCartScreen() throws {
        let app = launch()
        XCTAssertTrue(awaitElement(app, "cart.continue"))
        try audit(app, screen: "Cart")
    }

    func testCheckoutSummaryScreen() throws {
        let app = launch()
        tap(app, "cart.continue")
        XCTAssertTrue(awaitElement(app, "checkout.estimatedDeliveryLabel"))
        try audit(app, screen: "CheckoutSummary")
    }

    func testPaymentMethodScreen() throws {
        let app = launch()
        tap(app, "cart.continue")
        tap(app, "checkout.payment")
        XCTAssertTrue(awaitElement(app, "payment.disclosure"))
        try audit(app, screen: "PaymentMethod")
    }

    func testScanCardScreen() throws {
        let app = launch()
        tap(app, "cart.continue")
        tap(app, "checkout.payment")
        tap(app, "payment.scanCard")
        XCTAssertTrue(awaitElement(app, "scanCard.start"))
        try audit(app, screen: "ScanCard")
    }

    /// The control. This screen must produce zero audit issues; a finding here is a
    /// false positive and the eval corpus scores it as one.
    func testOrderConfirmationScreen() throws {
        let app = launch()
        tap(app, "cart.continue")
        tap(app, "checkout.payment")
        tap(app, "payment.placeOrder")
        XCTAssertTrue(awaitElement(app, "confirmation.viewHistory"))
        try audit(app, screen: "OrderConfirmation")
    }
}
