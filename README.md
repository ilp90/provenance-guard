# Provenance Guard

A pluggable backend that classifies whether submitted text-based creative work
reads as human-written or AI-generated, scores confidence in that call, surfaces
a transparency label to readers, and lets creators appeal a verdict.

Full design and rationale live in [planning.md](planning.md).

## Status

- **Milestone 1** — architecture ✅
- **Milestone 2** — spec ✅
- **Milestone 3** — submission endpoint + Signal 1 (Groq LLM) + audit log ✅
- Milestone 4 — Signal 2 (stylometry) + real confidence scorer — _next_
- Milestone 5 — transparency labels, appeals, tuned rate limits — _later_

> Confidence and the transparency label are **placeholders** until M4/M5 — they
> are currently derived from Signal 1 alone.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=your_key_here" > .env   # never commit .env
```

## Run

```bash
python app.py          # serves on http://127.0.0.1:5001
```

> **Port 5001, not 5000:** on macOS the AirPlay Receiver (AirTunes) occupies
> port 5000 and returns `403` to `localhost` requests, so the app uses 5001.

## Endpoints (current)

| Method | Path       | Purpose |
|--------|------------|---------|
| `POST` | `/submit`  | Classify a piece of text. Body: `{ "text": "...", "creator_id": "...", "title"?: "..." }` |
| `GET`  | `/log`     | Recent structured audit-log entries (`?limit=N`) |
| `GET`  | `/health`  | Liveness |

### Example

```bash
curl -s -X POST http://localhost:5001/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "The sun dipped below the horizon, painting the sky in hues of amber and rose. I sat on the porch, coffee in hand, watching the neighborhood slowly go quiet.", "creator_id": "test-user-1"}'
```

Returns `content_id`, `attribution`, `confidence` (placeholder), `signals.llm`,
and a placeholder `label`. Save the `content_id` — the appeals endpoint (M5)
uses it.

## Audit log

Every submission writes a structured row (SQLite, `provenance.db`) with the
content id, creator, timestamp, attribution, confidence, and Signal 1 score.
View it via `GET /log`. Sample output with 3+ entries is committed once M4/M5
finalize the schema.
