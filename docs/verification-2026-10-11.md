# Verification pass: 2026-10-11 (acceptance tier, Phase 0)

The acceptance-tier brief (`docs/briefs/0002-acceptance-tier.md`) rests on seven
premises nobody had checked. This file checks them. They cover how app logs reach a
result bundle, how a crash shows up, which XCUITest APIs exist, and what the fixture
actually renders.

Local checks ran on **Xcode 27.1 (27A9275)** with `xcresulttool` version 25115 (default
schema 0.4.0). The simulator was `Sightline iPhone SE (3rd generation)` on iOS 26.5.

**Method.** I made a throwaway copy of `eval/fixtures/CheckoutDemo` with:
- `Logger` lines at every level in `App.init`;
- an `order_submitted` line in `OrderConfirmationView.onAppear`;
- an opt-in `fatalError`.

Three UI tests drove it: two launches in one method, a crash, and a French locale. All
evidence came from the resulting bundle. Excerpts are committed under
`tests/fixtures/xcresult/acceptance-spike/`.

**Result: five of seven hold as planned, and two change the design.** App output
doesn't come one file per launch, and `.debug` lines are never captured.

| # | Question | Verdict |
|---|---|---|
| V1 | Do app `Logger` lines reach the `.xcresult`? | ⚠️ **Yes for `.info`/`.notice`/`.error`. `.debug` never appears** |
| V2 | Are non-public interpolations redacted? | ⚠️ **No. Even `privacy: .private` arrives in clear text** |
| V3 | One output file per `app.launch()`? | ❌ **No. One file per test run, every launch in it** |
| V4 | How does a crash appear? | ✅ A failure, two activities, and the `.ips` as an attachment. Output up to the crash survives |
| V5 | XCUITest APIs the driver needs | ✅ All present, including `waitForNonExistence(timeout:)` |
| V6 | Fixture ground truth `Subtotal $441.00` | ✅ Under `en_US`. **`fr_FR` renders `Subtotal US$441,00`** |
| V7 | Does schema pin 0.1.0 still parse on Xcode 27.1? | ✅ Accepted. CI's Xcode is unchecked |

---

## V1: app logs in the result bundle ⚠️

`xcresulttool export diagnostics --path <bundle> --output-path <dir>` writes
`…/StandardOutputAndStandardError-<bundle id>.txt`. It holds the app's stdout and
stderr, with unified-log output mirrored into it:

```
2026-10-11 13:13:27.764809-0400 CheckoutDemo[18307:951164] [acceptance] SIGHTLINE-AC|order_submitted|total=$441.00
```

| Level | Present |
|---|---|
| `.debug` | **no** |
| `.info` | yes |
| `.notice` | yes |
| `.error` | yes |
| `print()` | yes (no timestamp, no PID) |

`xcresulttool get log --type console` returns "No console log available" for a test
action. It doesn't apply here.

**Consequence.**
- The convention is `.notice`. A `SIGHTLINE-AC|` line logged at `.debug` would be
  invisible, and a "should happen once" criterion would read as observed zero: a false
  fail.
- The skill must write `.notice`, and ADR-0004 must say why.
- `print()` lines can't be attributed (V3), so they don't count as evidence.

## V2: privacy redaction ⚠️

`\(secret)`, `\(secret, privacy: .private)` and `\(secret, privacy: .public)` all
appear in clear text. The stderr mirror is not subject to `<private>` redaction.

**Consequence.** The convention doesn't need `privacy: .public` for capture to work.
The real risk runs the other way: whatever the app logs ends up in the evidence store
unredacted. Sightline therefore:
- stores only the attributed `SIGHTLINE-AC|` lines, never the whole file;
- runs them through `redact_text` (`core/evidence/redaction.py`) first.

The full file is about 67,000 lines for three tests, almost all XCTest agent chatter.
It's too big to be evidence anyway.

## V3: per-launch attribution ❌

**One file for the whole test run.** Four launches across three test methods, with PIDs
18307, 18322, 18329 and 18364, all write into the same file. Its header names only the
first PID. The plan assumed a file per launch; that's wrong.

What we do have:
- Every unified-log line carries `Process[PID:TID]` and a timestamp with a UTC offset.
- Every XCTest activity carries `startTime` (epoch seconds). Each `app.launch()`
  produces an `Open <bundle id>` activity with a `Launch <bundle id>` child.
  `app.terminate()` produces `Terminate <bundle id>:<pid>`.
- XCUIApplication exposes **no** public PID (`XCUIApplication.h` checked; there is
  `state` and `waitForState:timeout:`, but no process identifier).

**Design.** Attribute by time window, then confirm by PID.
- The window runs from this criterion's `Launch` activity `startTime` to the driver's
  `done` record. If there's no `done` record, it ends at the test's `Tear Down`.
- The unified-log lines in the window should all share one PID.
- **Exactly one PID in the window means capture is proven live for that launch.**
  XCTest's own in-process agent logs `[Default] Received request to exchange
  capabilities` from the app's PID at every launch, so a live process always has at
  least one line.
- **Zero PIDs or more than one** → `not_verifiable(log_unavailable)`.

Under this rule, the liveness check from the brief's addendum no longer needs an
app-side heartbeat event.

The criteria run serially in one test run on one machine clock. Activity times and log
times share that clock, so the window comparison is sound. The test is skipped under
`-parallel-testing-enabled YES`, which the acceptance scheme never turns on.

## V4: crashes ✅

`testB_crash` called `fatalError` in `onAppear`. In the `.xcresult`:
- `tests`: the test case has a `Failure Message` child, `com.sightline.fixtures.CheckoutDemo
  crashed`.
- `activities`: after `Tear Down`, there's `Checking for reports corresponding to
  termination of 1 app(s).`, with children `com.sightline.fixtures.CheckoutDemo crashed`
  (`isAssociatedWithFailure: true`) and `Crash: CheckoutDemo`.
- The crash log is an **attachment**, `Crash Log <date>.ips`, `lifetime: keepAlways`, so
  `export attachments` already retrieves it. It becomes `ArtifactKind.CRASH_REPORT`.
- The app output up to the crash survives: `SIGHTLINE-AC|about_to_crash`, then
  `Fatal error: spike crash`.

Diagnostics also contain stale `.ips` files from unrelated system daemons on other
days. Don't scan diagnostics for crash reports; use the attachment.

## V5: XCUITest APIs (Xcode 27.1 headers) ✅

Checked in `XCUIAutomation.framework/Headers`:
- `XCUIElementAttributes.h`: `identifier`, `value` (nullable `id`), `label`,
  `enabled` (`isEnabled`), `title`, `placeholderValue`, `selected`, `hasFocus`.
- `XCUIElement.h`: `exists`, `waitForExistenceWithTimeout:`,
  **`waitForNonExistenceWithTimeout:`**, `hittable`, `typeText:`.
- `XCUIElementQuery.h`: `count`, `matchingIdentifier:`, `element`,
  `allElementsBoundByIndex`.
- `XCUIApplication.h`: `launchArguments`, `launchEnvironment` (`[String: String]`),
  `waitForState:timeout:`.

Observed at runtime:
- `matching(identifier: "cart.removeItem").count == 3` on the Cart screen. The
  shared-identifier trap is real, and `count` detects it.
- A `StaticText`'s `value` is an empty string (`Optional("")`). The driver reports
  `actual` for `value` as the string, or `null` when nil.
- `waitForNonExistence(timeout:)` returned `true` once the screen was pushed.
- A launch-environment value containing `"` reached the app without error.

## V6: ground truth and locale ✅

- With `-AppleLanguages (en) -AppleLocale en_US`, `checkout.subtotal` has label
  **`Subtotal $441.00`**, as the brief worked out.
- With `-AppleLanguages (fr) -AppleLocale fr_FR`, the same element reads
  **`Subtotal US$441,00`**.

Pinning language and locale at launch is required, not decoration. The driver always
passes both, from config (default `en` / `en_US`), and the report records them.

## V7: schema pin ✅ (locally)

`xcresulttool` 25115 defaults to schema 0.4.0. It accepts `--schema-version 0.1.0`
for `get test-results activities`, and every capture in this pass used the pin. The
acceptance parser **shares the existing pin** (`xcresult.py` `SCHEMA_VERSION`). One pin,
one place to bump.

Open: CI's Xcode (26.4.1 per P2) wasn't re-checked. It predates 27.1 and accepted 0.1.0
at P3, so the risk is low but unverified.

## Also confirmed in passing

- **Activity titles round-trip arbitrary text.** A title containing `|`, a literal
  newline, `🐱`, `\` and `"` came back byte-for-byte in the JSON. The driver still uses
  base64url JSON inside `SIGHTLINE-ACC|…` records, so the parser never splits on a
  delimiter that observed app text can contain. The round trip shows the encoding is a
  choice, not a necessity.
