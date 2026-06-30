# Provenance Guard — Planning

Backend service that any creative-sharing platform can plug into to classify
submitted text, score confidence in that classification, surface a transparency
label to readers, and let creators appeal a verdict they believe is wrong.

> **Milestone 1 scope:** architecture and design only — no implementation code.
> This document defines the contract every later component will implement.

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

## Architecture

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

## Checkpoint (Milestone 1)

- [x] Can describe the full path of a submission, naming every component
      (API → rate limiter → pipeline (2 signals) → scorer → label → audit log).
- [x] Chose 2 distinct signals (semantic LLM + structural stylometry) and
      documented what each captures **and** its blind spots.
- [x] Listed the API endpoints (`/submit`, `/appeal`, `/log`, `/health`).
- [x] Diagrammed both the submission and appeal flows with labeled arrows.


