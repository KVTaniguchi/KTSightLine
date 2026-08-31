# ADR-0002: Evidence and the falsifiability gate

- **Status:** Proposed
- **Date:** 2026-08-31
- **Deciders:** Kevin Taniguchi

## Context

"No comment without evidence" is the product. Stated as a guideline it will erode in
week three, when someone has a really good finding and no artifact. So it has to be
structural: the code path that posts a comment must be *unable* to accept an unverified
finding. Not "should not." Cannot.

Two separate ideas get conflated and must not be:

- **Evidence** — an artifact exists and is addressable. A screenshot proves a screenshot
  was taken. It does not prove the text was clipped.
- **Verification** — an independent, deterministic procedure examined the artifacts and
  agreed with the claim. This is the falsifiability gate, borrowed from `pr-af`.

A model that says "clipped" plus a screenshot is *evidence-decorated*, not verified. That
distinction is the whole ADR.

## Decision

### 1. `ArtifactRef` is content-addressed and immutable

```python
ArtifactRef(
    sha256: str,                 # content address; also the storage key
    kind: ArtifactKind,          # closed enum, below
    uri: str,                    # sightline://evidence/<sha256> — resolved by the store
    produced_by: str,            # runner/capability id, e.g. "simulator.capture.screenshot"
    run_id: str,
    context: dict[str, str],     # device, os, appearance, content_size, locale, branch
    bytes: int,
    created_at: datetime,
)
```

`ArtifactKind` is closed: `screenshot`, `screen_recording`, `console_log`, `xcresult`,
`crash_report`, `instruments_trace`, `metric_series`, `build_log`, `source_span`,
`static_analysis_report`, `render_diff`.

Properties that matter:

- **Content-addressed.** The same screenshot captured by two skills stores once and both
  reference it. Dedupe of evidence falls out for free, and a fingerprint over evidence
  is stable.
- **`context` is mandatory and structured.** "Screenshot" is useless; "screenshot,
  iPhone SE 3rd gen, iOS 26.0, dark, AX5, en-XA, head@abc123" is a reproduction recipe.
  This is what goes in the comment footer.
- **Immutable.** No annotating an artifact after the fact. Derived things (a diff image)
  are *new* artifacts whose `context` names their parents.

Retention: artifacts are uploaded as CI job artifacts with the run, and the comment links
to them there. We do not host a service in v1. Consequence: links expire on the forge's
artifact retention schedule, and the comment must still be readable after they do — so
the *claim* never depends on clicking the link.

**Redaction is a first-class step, not a footnote.** Screenshots of a real e-commerce app
in CI will contain seeded PII, order numbers, and tokens. The evidence store runs a
configured masking pass (region masks from config, plus a deny-list of accessibility
identifiers) *before* an artifact is written. An unmasked artifact never touches disk in
the store.

### 2. Two types, and only one of them can be posted

```python
class ProposedFinding(BaseModel):   # what a skill produces
    ...
    evidence: Annotated[list[ArtifactRef], Field(min_length=1)]

class VerifiedFinding(BaseModel):   # what the forge adapter accepts
    ...
    verified_by: str                # non-optional
    verdict: Verdict
```

`ForgeAdapter.post_review(findings: list[VerifiedFinding])`. There is no other signature.
A `VerifiedFinding` is constructible **only** by `core/verify/gate.py`, via a private
constructor guard — not by a skill, not by the CLI, not by a test helper that someone
copy-pastes into production. The type system is the enforcement mechanism the brief asked
for.

Note the brief's schema has `verified_by: str | None`. That optionality is the bug. If a
finding can exist in the postable type with `verified_by = None`, someone will pass it.
Split the types instead.

### 3. Four verifier classes, each with a required artifact kind

A verifier is a deterministic function `(ProposedFinding, EvidenceStore) -> Verdict`. It
is *adversarial by construction*: it tries to reject. Ambiguity resolves to reject.

| Verifier | Confirms by | Required evidence | Example skill |
|---|---|---|---|
| `structured_oracle` | A first-party tool independently reported the same issue at the same element/location | `xcresult`, `static_analysis_report` | accessibility audit; SwiftLint consumption |
| `differential_render` | Base-branch and head-branch renders of the same surface differ beyond threshold, in the direction the claim predicts | 2× `screenshot` + `render_diff` | Dynamic Type clipping; dark mode |
| `reexecution` | The failure reproduces on a second, independent run | 2× (`console_log` \| `crash_report`) | data races; constraint conflicts; migration crashes |
| `differential_metric` | Base and head measured **on the same runner in the same job**, delta exceeds noise floor by a configured factor | `metric_series` ×2 | launch time; memory; app size; warning delta |

A skill's `verifier:` field names one. A verifier that cannot find its required artifact
kind returns `reject(reason="missing_evidence")` — it does not fall back to trusting the
model.

**What happens to an unverifiable finding:** it is dropped. Not downgraded to a
"consider" comment, not posted as a low-severity nit. It is written to the trajectory as
`suppressed{reason, verifier, artifacts_present}` and counted. The suppression log is the
most valuable artifact we produce for our own development — it is the list of checks we
almost got right, and it is what tells us which verifier to build next.

**What the author sees** (decided 2026-08-31, D8): counts and reasons, collapsed in the
run summary — `3 findings suppressed — 2 missing_evidence, 1 no_baseline`. Never the
claim text. Surfacing suppressed claims in a fold is posting unverified findings with
extra steps, and the first time someone acts on one we lose the argument for the gate.
Counts alone still resolve the ambiguity that matters: the reader can tell "found
nothing" from "found things and killed them".

### 4. Fingerprints are content-derived

```
fingerprint = sha256(
    rule_id + "\0" +
    normalized_path + "\0" +
    enclosing_symbol + "\0" +      # from a Swift structural parse, not regex
    normalized_claim
)
```

`normalized_claim` = lowercase, collapse whitespace, strip all digits and quoted string
literals, strip file paths. "Truncates to 'Estimated de…' at AX5 on iPhone SE" and
"truncates to 'Estimated deliv…' at AX5 on iPhone SE" must hash identically, or a
one-pixel font change re-posts the comment.

`enclosing_symbol` is the fully-qualified declaration containing the line
(`CheckoutSummaryView.body`), resolved from a real parse. If the parse fails, fall back to
the file-level symbol `<file>` rather than to a line number — a coarser fingerprint
over-dedupes, which is the safe direction.

Line numbers appear in the `Finding` for *positioning only* and are excluded from the
fingerprint by construction. There is a test that asserts this: mutate the line, assert
the fingerprint is unchanged.

### 5. Screenshot comparison — decided, with the reasoning written down

The brief demanded a deliberate choice. Here it is, as a three-stage cascade:

1. **Normalize, then mask.** Fixed device scale, animations disabled
   (`UIView.setAnimationsEnabled(false)` in the UI test host), status bar overridden to a
   fixed time/battery/carrier via `simctl status_bar override`, and config-declared
   region masks over known-dynamic content. Most naive-diff noise is eliminated here, not
   in the comparator.
2. **Perceptual threshold, not pixel equality.** Compare in a perceptual color space with
   a per-pixel ΔE tolerance and a *contiguous-region* area threshold — an
   anti-aliasing halo along every glyph edge is thousands of scattered sub-threshold
   pixels and must not register; a clipped label is one contiguous block and must.
   Thresholds are per-skill, tuned on the eval corpus, and recorded in the trajectory.
3. **Vision model as classifier, never as detector.** When stage 2 flags a region, a
   vision call answers "is this clipping, occlusion, a color regression, or benign
   reflow?" It categorizes and writes the claim sentence. It is *never* asked "is there a
   problem here?" over a full screenshot.

**Why this order.** Stage 3 is the only nondeterministic step and it runs last, on a
bounded input, with a deterministic gate in front of it. If we let a vision model detect,
the run stops being replayable, cost scales with screen count instead of defect count,
and the false-positive rate becomes a function of prompt phrasing — which is precisely
the "40 comments, right 12 times" failure the brief says gets us muted.

**Why not snapshot testing (`swift-snapshot-testing`).** It requires committed baselines
per device/appearance/content-size combination, which is a maintenance tax on the repo
we are trying to help. Our baseline is the base branch, rendered in the same job. Same
comparison, no committed artifacts. We should still consume existing snapshot baselines
as evidence where a repo already has them.

## Alternatives considered

**Evidence as a convention enforced in review.** Loses immediately. It is the one rule
that cannot survive social enforcement.

**Confidence scores instead of a binary gate.** Loses because a threshold on a
model-produced score is a knob nobody can calibrate, and it reintroduces "post it
anyway, but hedge." Binary, with a suppression log, is honest.

**LLM-as-judge as the verifier.** Loses on circularity — the thing being checked and the
checker share failure modes — and on replayability. Constrained to stage-3 classification
above, where a deterministic detector has already fired.

**Post unverified findings collapsed into a single "possible issues" comment.** Tempting
as a recall safety valve. Rejected for v1: it trains readers that our comments are
suggestions. Revisit only once addressal rate on verified comments is measured and high.

## Consequences

**Good.** Precision is structurally defended. Every comment is reproducible from its
`context` block. Dedupe across pushes works. The suppression log becomes the roadmap.

**Bad.** Recall drops, possibly a lot, and some of what we drop will be real. We are
accepting that trade explicitly; the eval corpus measures how much.

**Bad.** `differential_*` verifiers double the runtime cost of every skill that uses
them — base and head both have to be built and driven. ADR-0003 has to pay for this.

**Bad.** Verifiers are now the hardest code in the repo and the place bugs hurt most: a
buggy verifier silently suppresses true findings and nobody notices, because the failure
mode is silence. Every verifier needs both positive and negative fixtures in `eval/`,
and the suppression log needs a periodic human read.

## Open questions

- Noise floor for `differential_metric` on GitHub-hosted runners is unknown and probably
  bad. Needs measurement before any performance skill ships. Until then, performance
  skills are `experimental` and do not post.
- Artifact retention beyond CI job expiry — an S3-backed store is the obvious v2, but the
  `EvidenceStore` interface must not assume a filesystem now.
