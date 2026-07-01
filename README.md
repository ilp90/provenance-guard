# Provenance Guard

A pluggable backend that classifies whether submitted text-based creative work
reads as human-written or AI-generated, scores confidence in that call, surfaces
a transparency label to readers, and lets creators appeal a verdict.

Full design and rationale live in [planning.md](planning.md).

## Contents
- [Setup & run](#setup--run)
- [API](#api)
- [Detection pipeline (two signals)](#detection-pipeline-two-signals)
- [Confidence scoring & uncertainty](#confidence-scoring--uncertainty)
- [Transparency label (three variants)](#transparency-label-three-variants)
- [Appeals workflow](#appeals-workflow)
- [Rate limiting](#rate-limiting)
- [Audit log](#audit-log)
- [Known limitations](#known-limitations)
- [Spec reflection](#spec-reflection)
- [AI usage](#ai-usage)
- [Portfolio walkthrough](#portfolio-walkthrough)

## Setup & run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=your_key_here" > .env   # never commit .env (it is .gitignored)
python app.py                              # serves on http://127.0.0.1:5001
```

> **Port 5001, not 5000:** on macOS the AirPlay Receiver (AirTunes) occupies
> port 5000 and returns `403` to `localhost` requests, so the app uses 5001.
> Replace `5000` with `5001` in any milestone curl command.

## API

| Method | Path      | Body / params | Purpose |
|--------|-----------|---------------|---------|
| `POST` | `/submit` | `{ text, creator_id?, title? }` | Classify text; returns verdict, confidence, both signals, and the label |
| `POST` | `/appeal` | `{ content_id, creator_reasoning }` | Contest a verdict; sets status `under_review` and logs the appeal |
| `GET`  | `/log`    | `?limit=N` | Recent structured audit-log entries |
| `GET`  | `/health` | — | Liveness |

Errors: `400` invalid input, `404` unknown `content_id`, `429` rate limit.

### Example

```bash
curl -s -X POST http://localhost:5001/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "ok so i finally tried that new ramen place downtown and honestly? underwhelming...", "creator_id": "test-user-1"}'
```

Returns `content_id`, `attribution`, `confidence`, `combined_ai_probability`,
`disagreement`, `signals.{llm,stylometry}`, and `label`. Save the `content_id` —
`/appeal` needs it.

## Detection pipeline (two signals)

Two **genuinely independent** signals — one semantic, one structural — so their
blind spots don't overlap. Details in [planning.md §2](planning.md).

| Signal | Kind | Measures | Blind spot |
|--------|------|----------|------------|
| **1. LLM classifier** (Groq `llama-3.3-70b-versatile`) | Semantic | Whether the text *reads* human: voice, idiosyncrasy vs. even, hedged, cliché-leaning AI prose. Returns AI-probability `[0,1]` + rationale. | Weak on short text; fooled by lightly-edited AI; can be biased against formal/non-native writing |
| **2. Stylometry** (pure Python) | Structural | Sentence-length variance (burstiness), average word length, punctuation density → one AI-probability `[0,1]`. | Meaning-blind: formal/technical human writing looks "AI-like"; noisy on short input |

Because they measure different things, agreement → confidence, and
**disagreement → an honest `uncertain`** rather than a guess.

> **Why not type-token ratio?** We evaluated TTR and dropped it: on the short
> texts this system sees it was 0.86–0.90 for human *and* AI samples alike
> (length-dominated), carrying no signal. Average word length replaced it as a
> discriminating, still meaning-blind metric.

**Why these two signals?** Perfect AI detection is unsolved, so the goal isn't a
single oracle — it's *two views that fail differently.* The LLM reads meaning but
is a black box that can be confidently wrong; stylometry is transparent and
deterministic but meaning-blind. Pairing a semantic judge with a structural one
means the cases where each is weak (short text and light edits for the LLM;
formal human prose for stylometry) rarely coincide, so their **disagreement
becomes a usable signal in itself** — it's what drives the system to say
"uncertain" instead of guessing.

**What I'd change deploying this for real:**
- The LLM signal returns a point estimate at `temperature=0`; I'd sample it a few
  times and use the variance as a reliability weight (an unstable answer should
  count for less), rather than trusting one call.
- Stylometry cutoffs are hand-calibrated on a handful of texts. I'd fit them on a
  labelled human/AI corpus per genre (poetry vs. prose vs. technical writing),
  since "burstiness" norms differ sharply between forms.
- I'd add a third, provenance-based signal (submission metadata, edit history,
  keystroke cadence) — far harder to spoof than surface text statistics.

## Confidence scoring & uncertainty

The system never forces a binary output. Both signals produce an AI-probability;
they are combined and mapped to a verdict with **asymmetric thresholds** — a high
bar to accuse AI, a generous human band — because on a writing platform a false
positive (a human's work flagged AI) is the worst outcome.

```
combined = 0.60 * llm + 0.40 * stylometry          # LLM weighted higher (more reliable)
disagreement = |llm - stylometry|
confidence = clamp01( |combined - 0.5| * 2 - disagreement )
```

| `combined` | Verdict | Notes |
|------------|---------|-------|
| `<= 0.35`  | `likely_human` | generous human band |
| `0.35–0.70`| `uncertain` | the honest "we don't know" zone |
| `>= 0.70`  | `likely_ai` | high bar before accusing |

Two extra guards push toward caution:
- **Disagreement override:** if `disagreement >= 0.40`, the verdict is forced to
  `uncertain` regardless of the combined score.
- **Confidence gate:** a `likely_ai`/`likely_human` verdict only earns a
  confident *label* when `confidence >= 0.50`; otherwise the reader sees the
  neutral `uncertain` label. So a low-confidence AI verdict never reaches a
  reader as an accusation.

**Why this scoring approach?** I decided what `0.5` should *mean to a reader*
before touching the math: it should mean "we don't know," not "50% likely AI." A
naive `combined >= 0.5 → AI` rule would flip a coin at the exact point of maximum
ignorance and, worse, would accuse a human the moment structure looked slightly
uniform. So the thresholds are deliberately asymmetric (a `0.70` bar to say AI, a
`0.35` band to say human) and confidence is defined as *distance from the
undecided middle, minus how much the two signals disagree.* That single formula
encodes the whole design principle — you can only be confident when the score is
decisive **and** the signals concur.

**What the score means to a user:** `0.51` combined lands in the uncertain band
(cautious, non-accusatory); `0.95` lands in high-confidence AI. They are
meaningfully different outputs — not a flip at 0.5.

**Two worked examples (real scores from testing) — high vs. lower confidence:**

- **High confidence.** Casual, bursty human text ("ok so i finally tried that new
  ramen place downtown and honestly? underwhelming…") → `llm=0.20`, `sty=0.09`,
  **combined 0.16, confidence 0.58** → *high-confidence human* label. Both signals
  strongly agree it's human, so confidence is high.
- **Lower confidence.** Lightly-edited-AI text ("I have been thinking a lot about
  remote work lately. There are genuine tradeoffs — …") → `llm=0.40`, `sty=0.56`,
  **combined 0.47, confidence 0.00** → *uncertain* label. The score sits near the
  middle and the signals mildly disagree, so confidence collapses to zero.

The gap (0.58 vs 0.00) is the point: the system reports genuine uncertainty
rather than manufacturing a verdict.

**How we tested it was meaningful** — four deliberately-chosen inputs
(`llm` / `sty` = individual signal AI-probabilities):

| Input | llm | sty | combined | confidence | outcome |
|-------|-----|-----|----------|-----------|---------|
| Clearly AI (uniform, formal) | 0.80 | 1.00 | **0.88** | 0.56 | high-confidence AI |
| Clearly human (casual, bursty) | 0.20 | 0.09 | **0.16** | 0.58 | high-confidence human |
| Borderline formal-human | 0.80 | 0.56 | 0.70 | 0.17 | verdict `likely_ai` but **label = uncertain** (confidence gate protects the writer) |
| Borderline lightly-edited AI | 0.40 | 0.56 | **0.47** | 0.00 | uncertain |

Clear separation between clearly-AI (0.88) and clearly-human (0.16), and the two
borderline cases correctly avoid a confident accusation. The scorer was also
unit-checked against every threshold boundary in
[scoring.py](scoring.py) / [planning.md §5](planning.md).

## Transparency label (three variants)

The label shown to a reader is chosen by `(verdict, confidence)` — see
[labels.py](labels.py). Verbatim text of all three variants:

| Variant | When shown | Exact text |
|---------|-----------|------------|
| **High-confidence human** | `likely_human` and `confidence >= 0.50` | ✅ Likely written by a human. Our automated checks found strong signs this was written by a person, and our two independent checks agree. This is an estimate, not a guarantee. |
| **High-confidence AI** | `likely_ai` and `confidence >= 0.50` | 🤖 Likely AI-generated. Our automated checks found strong signs this text was produced with an AI tool. This is an automated estimate, not a certainty — if you wrote this yourself, you can appeal and a human will review it. |
| **Uncertain** | any other case (`uncertain`, or confidence `< 0.50`) | ❔ Not enough signal to tell. Our checks couldn't confidently determine whether this was written by a person or an AI. Please treat this as undetermined — it is not a judgment that the work is AI-generated. |

Design notes: the human label reassures; the AI label always pairs the verdict
with the appeal route; the uncertain label explicitly states it is **not** an
accusation. All three are reachable — verified over HTTP with the inputs above.

## Appeals workflow

A creator who believes a verdict is wrong contests it via `POST /appeal` with
their `content_id` and `creator_reasoning`. The system: (1) validates the
submission exists (`404` if not), (2) flips its status to `under_review`, and
(3) logs the appeal — reasoning + a pointer to the original decision — as a new
audit entry. No automated re-classification; a human reviews the queue.

```bash
curl -s -X POST http://localhost:5001/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "a62b8328-...", "creator_reasoning": "I wrote this myself. I am a non-native English speaker and my writing style may appear more formal than typical."}'
```
```json
{ "content_id": "a62b8328-...", "status": "under_review",
  "appeal_logged": true,
  "message": "Appeal received. This submission is now under review by a human." }
```

A reviewer opening the queue (`GET /log`, filter `status == under_review`) sees
the appeal reasoning beside the original verdict, both signal scores, and the
label that was shown — enough to uphold or overturn without re-running detection.

## Rate limiting

Flask-Limiter, applied to `POST /submit`, keyed by client IP
([app.py](app.py)):

```python
@limiter.limit("10 per minute;100 per day")
```

**Chosen limits: `10 / minute` and `100 / day`. Reasoning:**
- A real writer submits their *own* work — a handful of pieces, occasionally
  re-submitting an edited draft. Even heavy legitimate iteration stays well
  under **10/minute**; nobody hand-writes ten distinct poems a minute.
- A flooding script (hundreds of requests/second) is stopped on the 11th request
  in a window — immediate protection with negligible impact on humans.
- **100/day** caps sustained, slow-drip abuse that stays under the per-minute
  limit, and bounds cost, since each submission makes a paid LLM API call.
- *Limitation:* IP-keyed, so a shared NAT could throttle several users and a
  distributed attacker could spread load. A production system would key on an
  authenticated creator id and add Redis-backed storage.

**Evidence** — 12 rapid requests against the 10/min limit (first 10 pass, rest
are rejected):

```
200
200
200
200
200
200
200
200
200
200
429
429
```
```json
// body of a 429 response
{ "error": "rate limit exceeded", "limit": "10 per 1 minute",
  "message": "Too many submissions. Please slow down and try again shortly." }
```

## Audit log

Every decision and appeal is written to a structured SQLite log
([audit_log.py](audit_log.py)), viewable at `GET /log`. Each entry records:
timestamp, `content_id`, `creator_id`, `event_type` (`classification` |
`appeal`), attribution, confidence, **both individual signal scores**, status,
and a `detail` blob (LLM rationale, stylometry features, combined score,
disagreement — or, for appeals, the `appeal_reasoning` + original decision).

Full sample committed at [docs/audit-log-sample.json](docs/audit-log-sample.json)
(4 entries: 3 classifications + 1 appeal). Abridged:

```jsonc
[
  { "event_type": "appeal", "content_id": "a62b8328-...", "status": "under_review",
    "attribution": "likely_ai", "confidence": 0.56, "llm_score": 0.8, "stylometry_score": 1.0,
    "detail": { "appeal_reasoning": "I wrote this myself... non-native English speaker...",
                "original_decision": { "attribution": "likely_ai", "confidence": 0.56,
                                       "llm_score": 0.8, "stylometry_score": 1.0 } } },

  { "event_type": "classification", "content_id": "4bf07b39-...", "status": "classified",
    "attribution": "uncertain", "confidence": 0.0, "llm_score": 0.4, "stylometry_score": 0.565,
    "detail": { "combined_ai_probability": 0.466, "disagreement": 0.165, "high_confidence": false } },

  { "event_type": "classification", "content_id": "a62b8328-...", "status": "classified",
    "attribution": "likely_ai", "confidence": 0.56, "llm_score": 0.8, "stylometry_score": 1.0,
    "detail": { "combined_ai_probability": 0.88, "disagreement": 0.2, "high_confidence": true } },

  { "event_type": "classification", "content_id": "388bc91f-...", "status": "classified",
    "attribution": "likely_human", "confidence": 0.579, "llm_score": 0.2, "stylometry_score": 0.093,
    "detail": { "combined_ai_probability": 0.157, "disagreement": 0.107, "high_confidence": true } }
]
```

## Known limitations

Perfect AI detection is unsolved; this system is built to be *honest about
uncertainty*, not infallible. Specific failure modes, tied to the properties of
the signals:

- **Formal, technical, or non-native-English human writing is the signal's
  worst case.** Stylometry keys on long words, even sentence length, and dense
  punctuation — precisely the surface features of a well-edited academic abstract
  or a careful non-native writer. In testing, a genuine human paragraph on
  monetary policy scored `combined = 0.70` because *both* signals (not just
  stylometry — the LLM leaned AI too) read its evenness as machine-like. The only
  thing standing between that writer and a false accusation is the confidence
  gate, which drops the reader-facing label to "uncertain." That's a mitigation,
  not a fix: the underlying signals are genuinely fooled by formal prose, because
  neither can distinguish "disciplined human craft" from "AI uniformity."
- **Lightly human-edited AI text (false negative).** A few human edits raise
  burstiness and soften the LLM's cues, pushing text toward "uncertain" or even
  "human." Both signals operate on the final surface text and have no access to
  authorship history, so post-hoc editing is a blind spot by construction.
- **Very short text** (a haiku, a two-line microfiction): stylometric statistics
  are meaningless on ~15 words and the LLM has little to judge, so scores are
  noise. The system leans "uncertain" here rather than pretend to a verdict.

## Spec reflection

- **Where the spec helped:** writing the confidence formula and the three label
  variants in `planning.md` *before* coding forced the hardest decision up front —
  what `0.5` should mean to a reader. Because "0.5 = we don't know" was settled in
  the spec, the asymmetric thresholds, the disagreement override, and the
  confidence gate all fell out naturally instead of being retrofitted. The
  implementation was mostly transcription of a decision already made.
- **Where the implementation diverged:** the spec (§2.1) originally named
  type-token ratio as the third stylometric feature. When I measured it on real
  inputs it was 0.86–0.90 for human *and* AI text alike — length-dominated on
  short submissions, so it carried no signal. I replaced it with average word
  length (a strong, still meaning-blind discriminator) and updated `planning.md`
  to record the change and the evidence, keeping spec and code in sync rather than
  letting them drift.

## AI usage

This project was built with AI assistance (Claude). Notable instances where I
directed it and then revised or overrode the output:

1. **Stylometry signal + scorer generation.** I gave the AI the detection-signals
   and uncertainty sections of `planning.md` and asked it to implement Signal 2
   and the confidence scorer. It initially wired in type-token ratio exactly as
   the spec said. I directed it to *measure the raw features on the milestone test
   inputs first* — that surfaced TTR's uselessness on short text, and I overrode
   the spec, swapping in average word length and re-tuning the cutoffs against
   real numbers instead of accepting the plausible-but-flat default.
2. **The `429` response.** The AI's first rate-limit implementation returned
   Flask's default HTML error page. I overrode it with a JSON error handler so the
   endpoint stays machine-readable and consistent with the rest of the API, matching
   the planning-doc claim that every outcome (including `429`) is structured.
3. **macOS port diagnosis.** When `/submit` returned an empty `403`, I had the AI
   investigate rather than assume a code bug; it identified macOS AirPlay
   (`AirTunes`) squatting on port 5000 and moved the app to 5001 — a documented
   environment fix, not a workaround hidden in the code.

The design decisions (two-signal choice, asymmetric thresholds, the confidence
gate as the false-positive backstop) originated in the spec and were reviewed and
verified at each step, not accepted blindly from generated code.

## Portfolio walkthrough

A short (~2 min) video tour is linked here: _[add link]_. It shows a submission
flowing through both signals to a label, an appeal moving a decision to
`under_review`, and the rate limiter returning `429` — with a few words on why the
scoring is asymmetric.
