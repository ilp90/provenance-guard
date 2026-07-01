"""Provenance Guard — Flask API.

Milestone 3 scope:
  POST /submit  — accept text, run Signal 1 (LLM), persist + audit, respond.
  GET  /log     — return recent structured audit-log entries.
  GET  /health  — liveness.

Confidence and the transparency label are PLACEHOLDERS here — they are derived
from Signal 1 alone. Milestone 4 adds Signal 2 + the real combined scorer, and
Milestone 5 adds the final labels, appeals, and tuned rate limits.
See planning.md for the full contract.
"""
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

import audit_log
from detection import llm_signal

app = Flask(__name__)

# Rate limiting — placeholder default; specific per-endpoint limits + reasoning
# are chosen and documented in Milestone 5.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["60 per minute"],
)

audit_log.init_db()

MAX_TEXT_CHARS = 20000


# --- placeholder scoring (single-signal) — replaced by the real scorer in M4 ---
def _placeholder_attribution(llm_score):
    """Map Signal 1's score to a verdict using planning.md §5 thresholds.

    Asymmetric on purpose: high bar (>=0.70) before we ever say 'likely_ai'.
    """
    if llm_score >= 0.70:
        return "likely_ai"
    if llm_score <= 0.35:
        return "likely_human"
    return "uncertain"


def _placeholder_confidence(llm_score):
    # decisiveness = distance from the undecided middle. Real formula (with the
    # second signal's disagreement term) lands in M4.
    return round(abs(llm_score - 0.5) * 2, 3)


def _placeholder_label(attribution):
    text = {
        "likely_ai": "Placeholder: this text may be AI-generated (single-signal, pre-M5).",
        "likely_human": "Placeholder: this text appears human-written (single-signal, pre-M5).",
        "uncertain": "Placeholder: attribution undetermined (single-signal, pre-M5).",
    }[attribution]
    return {"variant": attribution, "text": text}


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/submit")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = data.get("creator_id")
    title = data.get("title")

    if not text:
        return jsonify({"error": "field 'text' is required and must be non-empty"}), 400
    if len(text) > MAX_TEXT_CHARS:
        return jsonify({"error": f"'text' exceeds {MAX_TEXT_CHARS} characters"}), 400

    content_id = str(uuid.uuid4())

    try:
        signal1 = llm_signal(text)
    except Exception as exc:  # noqa: BLE001 - surface upstream failures cleanly
        return jsonify({"error": "detection signal unavailable",
                        "detail": str(exc)}), 502

    llm_score = signal1["ai_probability"]
    attribution = _placeholder_attribution(llm_score)
    confidence = _placeholder_confidence(llm_score)
    label = _placeholder_label(attribution)

    audit_log.record_classification(
        content_id=content_id,
        creator_id=creator_id,
        text=text,
        title=title,
        attribution=attribution,
        confidence=confidence,
        llm_score=llm_score,
        stylometry_score=None,
        status="classified",
        detail={"llm_rationale": signal1["rationale"]},
    )

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,          # placeholder until M4
        "combined_ai_probability": llm_score,
        "signals": {
            "llm": {"ai_probability": llm_score,
                    "rationale": signal1["rationale"]},
            "stylometry": None,            # arrives in M4
        },
        "label": label,                    # placeholder until M5
        "status": "classified",
    })


@app.get("/log")
def log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": audit_log.get_log(limit=limit)})


if __name__ == "__main__":
    # Port 5001, not 5000: on macOS the AirPlay Receiver (AirTunes) squats on
    # port 5000 and returns 403 to localhost requests. Disable it in
    # System Settings > General > AirDrop & Handoff if you prefer 5000.
    app.run(host="127.0.0.1", port=5001, debug=True)
