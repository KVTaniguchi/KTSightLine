# Verification pass: 2026-10-20 (the sensor loop)

Four open items about what the sensor can and cannot see. Two were premises recorded in
`open-questions.md` that turned out to be wrong or unmeasured; one is answered here for
the first time; one cannot be answered on this machine.

Local checks ran on **Xcode 27.1 (27A9275)** with `xcresulttool` version 25115 (default
schema 0.4.0, pin 0.1.0). The simulator was `Sightline iPhone SE (3rd generation)` on
iOS 26.5.

| # | Question | Verdict |
|---|---|---|
| W1 | Does `textClipped` arrive with `element = nil`? | ❌ **No. The recorded premise was wrong** — it has a label and a frame, and no identifier |
| W2 | Is `simctl status_bar override` enough for byte-stable renders? | ✅ **Yes.** 3/3 screens byte-identical across 3 runs, 17 days, 2 device erases |
| W3 | Can camera / notification permissions be denied? | ⚠️ Mechanism built (D21); **not measured on a device** — the fixture crashes rather than prompting |
| W4 | Does the schema pin parse on CI's Xcode 26.4.1? | ⏸️ **Unanswerable locally.** Only 27.1 is installed |

---

## W1: `textClipped` is attributable ❌ (premise was wrong)

`open-questions.md` and `eval/fixtures/CheckoutDemo/GROUND_TRUTH.yaml` both recorded, on
2026-08-31, that `textClipped` "arrives with `element = nil`, so it collapses into
OQ-FIXTURE-1 and cannot be anchored". The committed fixture
(`tests/fixtures/xcresult/activities-cart.json`) contradicts it:

```
SIGHTLINE|Cart|textClipped|id=|label=Price $185.00|frame=(252.5, 256.75, 66.5, 20.5)|Text clipped
SIGHTLINE|Cart|hitRegion|id=|label=|frame=nil|Hit area is too small
```

The first has an element; it simply carries no `accessibilityIdentifier`. The second is
the genuinely element-less case. `is_attributable` tested `bool(identifier)`, which
cannot tell them apart, so both were discarded.

Anchoring the first class by matching its label against the source string literals
recovers it (D19). `CartView.swift:21` is
`.accessibilityLabel("Price \(item.formattedPrice)")`, and the literal segment `Price `
identifies the line without guessing. **Consequence:** Dynamic Type clipping is not
permanently a `differential_render` job, and `dynamic-type-clipping` can use
`structured_oracle` for the issues the audit does report.

**Found in passing, and it constrains the fix.** Enabling label anchoring for every
audit type cost an immediate false positive on eval corpus case 003 — the "clean" case:

```
contrast | id= | label=Contact support | frame=(16.0, 400.0, 343.0, …) | Contrast failed
```

That is not a mis-match. The audit correctly reports a contrast failure against the
inner label of a stock `.buttonStyle(.bordered)` button: tinted text on a near-white
fill, roughly 3.5:1 against a 4.5:1 threshold. The finding is true and must still not
post, because it is a property of a system button style rather than of the diff —
every adopting repo would get it on every `.bordered` button. So the strategy is an
allowlist of one audit type (D20), and the fixture's clean case is clean only because
`contrast` is excluded. See OQ-CONTRAST-1.

## W2: renders are byte-stable across boots ✅

ADR-0003 asked whether `simctl status_bar override` is sufficient or whether the status
bar region also needs masking. It is sufficient, and masking is not needed.

**Method.** Export the driver's screenshot attachments from the `.xcresult` with
`xcresulttool export attachments` and sha256 the PNG bytes — the artifact a
`differential_render` verifier would actually compare, not a `simctl io screenshot`.
Three independent runs of `sightline eval --suite review`, each on its own boot:

| Run | When | Device state |
|---|---|---|
| A | 2026-10-03 17:06 | a separate session, 17 days earlier |
| B | 2026-10-20 19:52 | after `simctl shutdown` + `simctl erase` |
| C | 2026-10-20 20:0x | after a second `erase`, fresh build, separate workdir |

| Screen | Bytes | sha256 (identical in A, B and C) |
|---|---|---|
| CheckoutSummary | 91220 | `de658f437b98ce29e41a862367f6ecd71e4e3faa5e6eb9afbeb24dc920275c54` |
| PaymentMethod | 74423 | `47d908c023321744380d382bea721a92c8d335efd9328cfd356fe7b5e9d3dcee` |
| OrderConfirmation | 86108 | `b2abfd8a25f4013583a843bb2baa1897ed36c6a7ca11b18c752d226bd173d3b8` |

The attachment filenames carry a fresh UUID per run (`…_0_55011E9B…` vs `…_0_1BE92B2B…`),
which is how you tell three real captures from one file read three times. **This was the
first attempt's bug:** `--workdir` defaults to `.sightline/run/eval`, not
`.sightline/eval`, so the first comparison hashed one stale 2026-10-03 bundle twice and
"proved" stability trivially.

**Scope of the claim.** Same host, same Xcode, same device type, same runtime, content
size `large`, light appearance. It does *not* establish cross-runner stability — and
ADR-0002 forbids cross-runner comparison anyway, so same-host is the only condition the
verifier ever runs in. Whether the 2026-10-03 run used this exact toolchain is not
recorded, so treat run A as corroboration and B/C as the controlled pair.

## W3: permission denial ⚠️ (mechanism built, not measured)

`simctl privacy`'s service list, re-confirmed on Xcode 27.1: calendar, contacts,
contacts-limited, location, location-always, photos, photos-add, media-library,
microphone, motion, reminders, siri. **Camera and notifications are absent**, as
recorded. `revoke` is still unverified *by effect* — D14 found `grant location` exits 0
and the prompt appears anyway.

So answering the prompt is the only mechanism for those two, and D14 already does half
of it. The gap was that `dismiss_alerts:` is one list, tapped first-match-wins, so it
answers uniformly and cannot deny. D21 adds `deny_alerts:` and
`sightline audit --deny-permissions`; the two lists are never merged, because a merged
list would tap whichever button the alert happens to show first and the condition under
test would depend on button order. A prompt with no configured deny title fails the run
rather than being granted — silently granting makes a permission-denied check pass for
the wrong reason.

**Not measured on a device.** The vendored fixture cannot exercise it: D-003
deliberately omits `NSCameraUsageDescription`, so `AVCaptureDevice.requestAccess`
crashes instead of prompting. Confirming it needs a probe app with valid usage
descriptions, or a real app that prompts — Nearby Meows prompts for location.

## W4: the schema pin on CI's Xcode ⏸️

Not answerable here: this machine has only Xcode 27.1, and V7 is about the runner's
26.4.1 (P2). Note that `sightline.yml`'s runtime job cannot answer it either on a
Python-only diff — `review.py` returns before building when no configured surface
matches the changed views, so no bundle is produced and nothing is parsed.

`ci.yml`'s `eval` job can: it runs on `macos-26` for a PR labelled `sightline:full`,
and builds, drives and parses with the pin. A `Report toolchain` step was added so the
run records the versions it actually had.
