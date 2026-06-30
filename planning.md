# Provenance Guard — Planning

Backend service that any creative-sharing platform can plug into to classify
submitted text, score confidence in that classification, surface a transparency
label to readers, and let creators appeal a verdict they believe is wrong.

> **Status:** Milestones 1–2 complete (architecture + full spec). This document
> is the contract every later component implements, and the source material fed
> to AI tools during M3–M5 (see the **AI Tool Plan** section).

---

## 1. Architecture Narrative — the path of one piece of text

This is the journey a single poem/story/blog excerpt takes through the system.

1. **Creator submits text** → `POST /submit` with the raw text (and optional
   `creator_id`, `title`). The **API layer** (Flask) validates the payload
   (non-empty, within a length bound) and assigns a `submission_id`.

2. **Rate limiter** (Flask-Limiter) checks the caller against the configured
   limits *before* any expensive work runs. If over the limit → `429` and the
   request stops here (still logged as a rejected attempt).

3. The text is handed to the **Detection Pipeline**, which runs **two
   independent signals**:
   - **Signal 1 — LLM classifier (Groq, llama-3.3-70b-versatile):** a semantic
     judgment of whether the text "reads as" AI-generated. Returns a
     probability-of-AI in `[0,1]` plus a short rationale.
   - **Signal 2 — Stylometric heuristics (pure Python):** structural statistics
     (sentence-length variance, type-token ratio, punctuation density). Returns
     a probability-of-AI in `[0,1]`.

4. **Confidence Scorer** combines the two signal scores into one
   `combined_ai_probability`, then derives:
   - a **verdict** — `likely_ai`, `likely_human`, or `uncertain`,
   - a **confidence** value (how far the combined score sits from the
     uncertain middle band — closeness to 0 or 1 = high confidence),
   - **agreement** between the two signals (signals disagreeing lowers
     confidence / pushes toward `uncertain`).

5. **Transparency Label builder** maps `(verdict, confidence)` to one of three
   plain-language label variants (high-confidence AI / high-confidence human /
   uncertain). This is the text a reader sees on the platform.

6. **Audit Log** (SQLite) records a structured row: `submission_id`, timestamp,
   verdict, combined score, **both individual signal scores**, the label shown,
   and the creator id. This row is the canonical, immutable record of the
   decision.

7. **Response** returns to the creator/platform: `submission_id`, verdict,
   confidence, both signal scores, and the transparency label text.

**Later, if the creator disputes the verdict:**

8. **Creator appeals** → `POST /appeal` with the `submission_id` and their
   written reasoning. The API validates the submission exists.

9. The submission's **status is updated to `under_review`**, and the appeal
   (reasoning + a pointer to the original decision) is written to the **Audit
   Log** as a new event. No automatic re-classification — a human reviews.

10. **Response** confirms the appeal was logged and the status is now
    `under_review`.

---

## 2. Detection Signals

The two signals are deliberately of *different kinds*: one **semantic**, one
**structural**. That independence is what makes the combination more
informative than either alone.

### Signal 1 — LLM Classifier (Groq, llama-3.3-70b-versatile)
- **Measures:** holistic semantic + stylistic coherence — does the text have
  the "voice," surprise, idiosyncrasy, and topical grounding of human writing,
  or the smooth, hedged, evenly-developed quality typical of AI generations?
- **Why it differs:** AI text tends to be relentlessly coherent, balanced, and
  cliché-leaning; human writing more often takes risks, makes leaps, and has a
  distinct personal register the model can recognize.
- **Blind spot:** unreliable on very short text; can be fooled by AI output
  that was lightly human-edited or by human writing that happens to be formal
  and tidy; the model is non-deterministic and can be over-confident; it can
  carry stylistic bias against non-native English or formulaic genres.

### Signal 2 — Stylometric Heuristics (pure Python)
- **Measures:** quantifiable structure of the text:
  - **sentence-length variance** (burstiness) — humans vary sentence length a
    lot; AI tends toward uniform medium-length sentences,
  - **type-token ratio** — vocabulary diversity per length,
  - **punctuation density** — rate/variety of punctuation marks.
- **Why it differs:** AI sampling smooths toward statistically average,
  low-variance prose; human writing is "burstier" and more uneven.
- **Blind spot:** purely surface-level — has no idea what the text *means*. A
  human writing in a deliberately uniform style (e.g. terse minimalism, a
  technical abstract) looks "AI-like"; a creative AI prompt can produce high
  variance. Statistics are noisy and unstable on short inputs.

**Why pairing them helps:** the LLM's blind spots (length, light editing) and
the stylometry's blind spots (no meaning, style false-positives) are different,
so when both agree we can be confident, and when they disagree the system
honestly reports `uncertain` rather than guessing.

### 2.1 Signal output shapes

Both signals output an **AI-probability in `[0,1]`** (0 = certainly human,
1 = certainly AI) so they're combinable on one scale.

```jsonc
// Signal 1 — LLM classifier
{ "ai_probability": 0.82, "rationale": "Even, hedged phrasing; no personal voice." }

// Signal 2 — Stylometry
{ "ai_probability": 0.31,
  "features": {
    "sentence_length_variance": 0.74,  // high = bursty/human-like
    "type_token_ratio": 0.58,          // high = diverse vocab
    "punctuation_density": 0.06        // marks per word
  } }
```

The LLM is prompted to return an integer 0–100 (÷100). Stylometry maps each raw
feature to a per-feature AI-score via documented thresholds (e.g. variance below
a cutoff → leans AI), then averages the three into one `ai_probability`.

### 2.2 Combination → `combined_ai_probability`

```
combined = 0.60 * llm_ai_prob + 0.40 * stylometry_ai_prob
disagreement = abs(llm_ai_prob - stylometry_ai_prob)   # 0..1
```

The LLM gets the higher weight (0.60) because semantic judgment is the more
reliable signal; stylometry (0.40) is a structural cross-check. `disagreement`
feeds the confidence / verdict logic in §5 — high disagreement pulls the verdict
toward `uncertain` regardless of where `combined` lands.

---

## 3. The False-Positive Problem (human work flagged as AI)

> On a writing platform, a **false positive — calling a real human's work
> AI-generated — is the worst outcome.** It accuses a creator and damages
> trust. The system is biased to *avoid* this.

**Trace:** A human poet submits a tidy, formal sonnet.

- Signal 2 (stylometry) sees low sentence-length variance → reports a high
  AI-probability (false alarm).
- Signal 1 (LLM) recognizes a genuine human voice → reports low AI-probability.
- **Signals disagree.** The confidence scorer detects the disagreement, lowers
  confidence, and the verdict lands in the **`uncertain`** band — *not*
  `likely_ai`.
- The **label** shown is the uncertain variant ("our tools couldn't confidently
  determine…"), which never asserts the work is AI.
- If the verdict had still leaned AI, the creator uses `POST /appeal` with their
  reasoning; status → `under_review`; the appeal + original decision are logged
  for a human to resolve.

**Design decisions this drives (for Milestone 2):**
- A wide `uncertain` band, so borderline scores never get an accusatory label.
- Signal disagreement explicitly pushes toward `uncertain`.
- A high score threshold required before showing the "high-confidence AI" label.
- The appeal path is always available and surfaced in the response.

---

## 4. API Surface (the contract)

| Method | Endpoint        | Accepts                                                        | Returns |
|--------|-----------------|---------------------------------------------------------------|---------|
| `POST` | `/submit`       | `{ text, creator_id?, title? }`                               | `{ submission_id, verdict, confidence, combined_ai_probability, signals: { llm, stylometry }, label: { variant, text }, status }` |
| `POST` | `/appeal`       | `{ submission_id, reason }`                                   | `{ submission_id, status: "under_review", appeal_logged: true }` |
| `GET`  | `/log`          | optional `?submission_id=` / `?limit=`                        | array of structured audit-log entries |
| `GET`  | `/health`       | —                                                             | `{ status: "ok" }` (liveness) |

Error contract: `400` invalid/empty input, `404` unknown `submission_id`,
`429` rate-limit exceeded. Every outcome (including `429`/`404`) is auditable.

---

## 5. Uncertainty Representation

The system never forces a binary verdict. It maps `combined_ai_probability` to
one of three verdicts using **asymmetric thresholds** — because a false positive
(human work flagged AI) is the worst outcome, the bar to accuse AI is set high
and the human band is generous.

| `combined_ai_probability` | Verdict         | Meaning |
|---------------------------|-----------------|---------|
| `<= 0.35`                 | `likely_human`  | Strong human signal |
| `0.35 < x < 0.70`         | `uncertain`     | The honest "we don't know" zone |
| `>= 0.70`                 | `likely_ai`     | High bar required before accusing |

**Disagreement override:** if `disagreement >= 0.40`, the verdict is forced to
`uncertain` even when `combined` falls in an outer band. The signals are telling
two different stories — we refuse to pick one.

**What 0.6 means:** `combined = 0.6` sits inside the uncertain band, so it is
*not* a verdict of "AI" — it produces the **uncertain** label. This is the
design hint made concrete: `0.51` → uncertain (cautious, non-accusatory), while
`0.95` → high-confidence AI. They are meaningfully different outputs, not a flip
at 0.5.

**Confidence value (0–1)** reported alongside the verdict, expressing how
decisive the call is:

```
confidence = clamp01( (abs(combined - 0.5) * 2) - disagreement )
```

- `abs(combined - 0.5) * 2`: 0 at the dead-center 0.5, 1 at the extremes.
- subtract `disagreement`: divided signals lower our confidence.

So `combined=0.95, disagreement=0.1` → confidence ≈ **0.80** (high-confidence
label); `combined=0.6, disagreement=0.3` → confidence ≈ **−0.1 → 0.0** (clearly
uncertain). A label is only "high-confidence" when `confidence >= 0.50`.

**Calibration / testing plan:** assemble a small fixture set — clearly-human
texts (personal essays, idiosyncratic poems), clearly-AI texts (raw model
output), and ambiguous ones (edited AI, formal human prose). Assert that
clearly-human scores land `<= 0.35`, clearly-AI `>= 0.70`, and ambiguous in the
middle. If a clearly-human sample scores as AI, widen the human band or down-
weight stylometry. Documented in the README's confidence section.

---

## 6. Transparency Label Design

Three variants, chosen by `(verdict, confidence)`. Each is plain-language,
states confidence in words a non-technical reader understands, and **never
accuses without offering the appeal path.** Verbatim text (also reproduced in
the README):

**High-confidence human** — `verdict == likely_human` and `confidence >= 0.50`:

> ✅ **Likely written by a human.** Our automated checks found strong signs this
> was written by a person, and our two independent checks agree. This is an
> estimate, not a guarantee.

**High-confidence AI** — `verdict == likely_ai` and `confidence >= 0.50`:

> 🤖 **Likely AI-generated.** Our automated checks found strong signs this text
> was produced with an AI tool. This is an automated estimate, not a certainty —
> if you wrote this yourself, you can appeal and a human will review it.

**Uncertain** — `verdict == uncertain` *or* `confidence < 0.50`:

> ❔ **Not enough signal to tell.** Our checks couldn't confidently determine
> whether this was written by a person or an AI. Please treat this as
> undetermined — it is not a judgment that the work is AI-generated.

Note the asymmetry: the human label is reassuring, the AI label always pairs the
verdict with the appeal route, and the uncertain label explicitly says "this is
**not** an accusation."

---

## 7. Appeals Workflow

- **Who can appeal:** the creator of a submission (identified by the
  `creator_id` supplied at submission, or by possession of the `submission_id`).
  Any verdict can be appealed, but it matters most for `likely_ai`.
- **What they provide:** `POST /appeal` with `{ submission_id, reason }` — the
  `reason` is free-text explaining why they believe the verdict is wrong (e.g.
  "I wrote this by hand, here's my drafting history"). Optional `contact`.
- **What the system does on receipt:**
  1. Validate the `submission_id` exists (else `404`).
  2. Update the submission's `status` from `classified` → `under_review`.
  3. Write a new **`appeal`** entry to the audit log, carrying the appeal
     `reason`, a timestamp, and a pointer to the original decision row (verdict,
     combined score, both signal scores, the label shown).
  4. Return `{ submission_id, status: "under_review", appeal_logged: true }`.
- **No automatic re-classification** — a human resolves it.
- **What a reviewer sees (appeal queue):** querying the audit log for
  `status == under_review` returns, per appeal: the original text (or a
  snippet + length), the verdict and `combined_ai_probability`, both individual
  signal scores + the LLM rationale, the exact label that was shown, the
  creator's appeal `reason`, and timestamps for both the decision and the
  appeal. Enough context to overturn or uphold without re-running detection.

---

## 8. Anticipated Edge Cases

Specific content this system will handle poorly, and why:

1. **Repetitive, simple-vocabulary verse (refrains, nursery rhyme, villanelle).**
   Heavy repetition crushes the type-token ratio and a fixed meter flattens
   sentence-length variance, so **stylometry reads it as AI** — yet the repetition
   is deliberate human craft. *Mitigation:* the LLM signal recognizes the poetic
   intent, and on disagreement the verdict falls to `uncertain`, not `likely_ai`.

2. **Very short text (a haiku, a 2-line microfiction, a tweet-length excerpt).**
   Stylometric statistics are meaningless on ~15 words (variance/TTR are noise),
   and the LLM is unreliable on so little context. *Mitigation:* a minimum-length
   guard — below the threshold the system returns `uncertain` with low confidence
   and a label that says so, rather than pretending to a verdict.

3. **Lightly human-edited AI text (false negative).** A creator who runs AI
   output through a human edit pass can wash out both signals — the structure
   looks more varied and the voice reads more natural. The system will likely
   call this `likely_human` or `uncertain`. This is an honest, documented limit;
   the audit log preserves the scores so patterns can be reviewed later.

4. **Non-native-English or highly formal/technical human writing.** Formulaic,
   even prose can trip stylometry toward AI and bias the LLM. *Mitigation:* the
   generous human band, disagreement→uncertain rule, and the appeal path guard
   against penalizing these writers.

---

## Architecture

**Narrative.** In the *submission flow*, raw text enters through the Flask API,
passes the rate limiter, and is scored by two independent signals (LLM +
stylometry); the confidence scorer combines those scores into a verdict and
confidence, the label builder turns that into reader-facing text, and the whole
decision is written to the SQLite audit log before the JSON response is returned.
In the *appeal flow*, a creator POSTs a `submission_id` and reason; the API flips
that submission's status to `under_review`, writes an `appeal` entry to the audit
log linked to the original decision, and returns confirmation — no automated
re-classification, a human reviews the queue.

### Submission flow

```
                          ┌─────────────────────────────┐
  raw text                │        Flask API layer        │
  POST /submit  ────────► │  validate + assign id         │
                          └───────────────┬───────────────┘
                                          │ raw text
                                  ┌───────▼────────┐   over limit
                                  │  Rate Limiter   │──────────► 429 (logged)
                                  └───────┬────────┘
                                          │ raw text
                          ┌───────────────▼───────────────┐
                          │       Detection Pipeline        │
                          │                                 │
                          │  ┌──────────────┐  ┌──────────┐ │
                          │  │ Signal 1     │  │ Signal 2 │ │
                          │  │ LLM (Groq)   │  │ Stylom.  │ │
                          │  └──────┬───────┘  └────┬─────┘ │
                          │   ai_prob_llm      ai_prob_sty   │
                          └─────────┴────────────────┴───────┘
                                          │ two signal scores
                          ┌───────────────▼───────────────┐
                          │      Confidence Scorer          │
                          │  combine + agreement →          │
                          │  combined_score, verdict,       │
                          │  confidence                     │
                          └───────────────┬───────────────┘
                                          │ verdict + confidence
                          ┌───────────────▼───────────────┐
                          │   Transparency Label builder    │
                          │  → variant + label text         │
                          └───────────────┬───────────────┘
                                          │ full decision record
                          ┌───────────────▼───────────────┐
                          │      Audit Log (SQLite)         │
                          │  id, ts, verdict, combined,     │
                          │  llm score, stylometry score,   │
                          │  label, creator_id, status      │
                          └───────────────┬───────────────┘
                                          │ JSON
                                          ▼
                       response: { verdict, confidence,
                                   signals, label, status }
```

### Appeal flow

```
  POST /appeal                ┌──────────────────────┐
  { submission_id, reason } ─►│   Flask API layer     │
                              │  validate id exists    │──── unknown id ──► 404
                              └───────────┬───────────┘
                                          │ submission_id + reason
                              ┌───────────▼───────────┐
                              │   Status update         │
                              │  status → under_review  │
                              └───────────┬───────────┘
                                          │ appeal event + link to original decision
                              ┌───────────▼───────────┐
                              │   Audit Log (SQLite)    │
                              │  new "appeal" entry      │
                              └───────────┬───────────┘
                                          │ JSON
                                          ▼
                  response: { status: "under_review", appeal_logged: true }
```

---

## AI Tool Plan

How each implementation milestone will use this spec to prompt an AI tool.

### M3 — Submission endpoint + first signal
- **Spec I'll provide:** §2 + §2.1 (Signal 1 shape), §4 (API contract), the
  Architecture diagram.
- **Ask for:** a Flask app skeleton (`/submit`, `/health`, `.env`/Groq client
  wiring) plus the Signal 1 function — text in, `{ ai_probability, rationale }`
  out — including the Groq prompt that asks for an integer 0–100.
- **Verify:** call the Signal 1 function directly on a few clearly-AI and
  clearly-human samples and eyeball that probabilities point the right way
  *before* wiring it into the endpoint; hit `/submit` with curl for shape.

### M4 — Second signal + confidence scoring
- **Spec I'll provide:** §2 + §2.1 (Signal 2 features), §2.2 (combination),
  §5 (thresholds, disagreement rule, confidence formula), the diagram.
- **Ask for:** the pure-Python stylometry function (variance, TTR, punctuation →
  one `ai_probability`) and the scorer implementing the exact formulas/thresholds
  in §2.2 and §5 (combined score, disagreement override, verdict, confidence).
- **Verify:** run the fixture set from §5 — assert clearly-human `<= 0.35`,
  clearly-AI `>= 0.70`, ambiguous in the middle, and that `0.51` vs `0.95`
  produce different verdicts. Scores must vary meaningfully, not flip at 0.5.

### M5 — Production layer (labels, appeals, rate limit, audit log)
- **Spec I'll provide:** §6 (verbatim label variants + selection rule), §7
  (appeals workflow), §3 (false-positive bias), the diagram.
- **Ask for:** the label builder mapping `(verdict, confidence)` → one of the
  three §6 strings, the `/appeal` endpoint (validate → status `under_review` →
  log), Flask-Limiter config, and the SQLite audit-log schema/writes + `/log`.
- **Verify:** craft inputs that reach each of the three labels; POST an appeal
  and confirm status flips to `under_review` and an `appeal` row links to the
  original decision; confirm `/log` shows ≥3 structured entries; confirm the
  rate limit returns `429` when exceeded.

---

## Checkpoint

- [x] **M1** — full submission path described, 2 distinct signals chosen with
      blind spots, endpoints listed, both flows diagrammed.
- [x] **M2** — all five questions answered with implementation-ready specifics:
  - [x] Detection signals: output shapes (§2.1) + combination formula (§2.2).
  - [x] Uncertainty: what 0.6 means, thresholds, confidence formula (§5).
  - [x] Three label variants written verbatim (§6).
  - [x] Appeals workflow incl. reviewer-queue view (§7).
  - [x] ≥2 specific edge cases (§8).
- [x] Confidence produces different labels across ranges, not a flip at 0.5 (§5).
- [x] `## Architecture` section has the M1 diagram + a flow narrative.
- [x] `## AI Tool Plan` covers M3/M4/M5 with sections, requests, verification.

## Open decisions deferred to implementation
- Final stylometry per-feature cutoffs (tune against fixtures in M4).
- Specific rate-limit numbers + reasoning — decide in M5, document in README.


