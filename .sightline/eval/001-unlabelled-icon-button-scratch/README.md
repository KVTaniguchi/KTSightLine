# CheckoutDemo — the eval fixture (D2)

A minimal SwiftUI checkout flow with five deliberately seeded defects and one
deliberately clean screen. Five screens, no dependencies, no asset catalog, no project
generator — the `.xcodeproj` is checked in and hand-maintained so the fixture builds
anywhere with only Xcode installed.

**Do not fix the defects.** `GROUND_TRUTH.yaml` scores precision and recall against
them.

## Build and run

```bash
xcrun simctl create "Sightline iPhone SE (3rd generation)" \
  com.apple.CoreSimulator.SimDeviceType.iPhone-SE-3rd-generation \
  com.apple.CoreSimulator.SimRuntime.iOS-26-5
```

```bash
xcodebuild test -project CheckoutDemo.xcodeproj -scheme CheckoutDemo -destination 'name=Sightline iPhone SE (3rd generation)'
```

Set the Dynamic Type size externally, the way the harness will:

```bash
xcrun simctl ui booted content_size accessibility-extra-extra-extra-large
```

## Screens

| Screen | Role |
|---|---|
| `CartView` | D-004 (unlabelled image button), D-005 (16×16 tap target) |
| `CheckoutSummaryView` | D-001 (fixed-height row that clips at AX5) |
| `PaymentMethodView` | D-002 (hardcoded grey on hardcoded white) |
| `ScanCardView` | D-003 (camera API, no `NSCameraUsageDescription`) |
| `OrderConfirmationView` | **Clean.** The regression test for "posts nothing" |

## What building this actually taught us

The fixture was designed on paper and then run. Three of the five defects behaved
differently from the design, and those differences are worth more than the fixture.

**1. `performAccessibilityAudit` does not catch Dynamic Type clipping in SwiftUI.**
D-001 is visibly destroyed at AX5 — "Estimated delivery" clipped mid-glyph on both
sides, the date row gone entirely — and the audit reports nothing, across four
different clipping shapes (`lineLimit` + fixed height, max-height + `clipped()`,
`fixedSize` + fixed width, `fixedSize` + max height). Compare
`reference-renders/CheckoutSummary-se-light-default.png` against
`...-ax5.png`.

Consequence: the cheap `structured_oracle` path cannot cover the defect class the
project's thesis leads with. Dynamic Type clipping has to go through
`differential_render` — base-vs-head screenshot comparison — exactly as the check
catalog already separates them. The architecture holds; the framing of "the audit is
our first slice" needs to be honest that the audit covers labels, contrast, and hit
regions, not clipping.

**2. Severity lives in `compactDescription`, not `auditType`.**
The audit distinguishes "Contrast failed" from "Contrast **nearly passed**". Treat
every reported issue as a finding and you post warnings as defects on every screen in
the app — including the deliberately-correct one. Filtering to `failed` is what makes
`OrderConfirmationView` clean.

**3. Many issues arrive with `element = nil`.**
No identifier, no frame, nothing to anchor a comment to — including D-002, a defect we
seeded on purpose. A `Finding` needs a file and a line. This is unresolved
(`OQ-FIXTURE-1`) and it constrains the accessibility skill's anchoring logic.

**4. Driving the app at AX5 is not the same as driving it at default size.**
The first AX5 run failed outright: "Continue to checkout" scrolls off-screen and the
tap found no match. A driver that only taps works at default size and silently fails
the entire AX5 matrix — the matrix the project exists to test. `CheckoutDemoUITests`
scrolls before tapping for this reason.

**5. Stock SwiftUI controls generate audit noise.**
`NavigationLink` labels report "Dynamic Type font sizes are partially unsupported";
section headers and bordered buttons report "Contrast nearly passed". These are
Apple's components, not the author's code. Suppression is not optional.

## Evidence path, confirmed working

```bash
xcodebuild test ... -resultBundlePath run.xcresult
xcrun xcresulttool export attachments --path run.xcresult --output-path ./out
```

Screenshots attached with `XCTAttachment(screenshot:)` and `lifetime = .keepAlways`
export cleanly with a `manifest.json` mapping ids to test identifiers. That is the
`screenshot` `ArtifactRef` producer for the evidence store.
