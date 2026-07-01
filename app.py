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
from detection import llm_signal, stylometry_signal
from labels import build_label
from scoring import score

app = Flask(__name__)

# Rate limiting. Limits are applied per-endpoint (see /submit). Keyed by client
# IP. In-memory storage is fine for this single-process dev/grading setup; a
# production deploy would point storage_uri at Redis. Reasoning for the chosen
# numbers is documented in the README.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# /submit limits — see README "Rate limiting" for the reasoning.
SUBMIT_LIMITS = "10 per minute;100 per day"

audit_log.init_db()

MAX_TEXT_CHARS = 20000


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "rate limit exceeded",
        "limit": str(e.description),
        "message": "Too many submissions. Please slow down and try again shortly.",
    }), 429


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/submit")
@limiter.limit(SUBMIT_LIMITS)
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

    signal2 = stylometry_signal(text)          # pure Python, no network
    llm_score = signal1["ai_probability"]
    sty_score = signal2["ai_probability"]

    result = score(llm_score, sty_score)       # planning.md §2.2 + §5
    attribution = result["verdict"]
    confidence = result["confidence"]
    label = build_label(attribution, confidence)

    audit_log.record_classification(
        content_id=content_id,
        creator_id=creator_id,
        text=text,
        title=title,
        attribution=attribution,
        confidence=confidence,
        llm_score=llm_score,
        stylometry_score=sty_score,
        status="classified",
        detail={
            "llm_rationale": signal1["rationale"],
            "stylometry_features": signal2["features"],
            "combined_ai_probability": result["combined_ai_probability"],
            "disagreement": result["disagreement"],
            "high_confidence": result["high_confidence"],
        },
    )

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,
        "combined_ai_probability": result["combined_ai_probability"],
        "disagreement": result["disagreement"],
        "signals": {
            "llm": {"ai_probability": llm_score,
                    "rationale": signal1["rationale"]},
            "stylometry": {"ai_probability": sty_score,
                           "features": signal2["features"]},
        },
        "label": label,
        "status": "classified",
    })


@app.post("/appeal")
def appeal():
    """A creator contests a classification (planning.md §7).

    Accepts { content_id, creator_reasoning }. Flips the submission to
    'under_review' and logs the appeal beside the original decision. No
    automated re-classification — a human reviews the queue.
    """
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    # Accept the milestone's field name and the planning.md alias.
    reasoning = (data.get("creator_reasoning") or data.get("reason") or "").strip()

    if not content_id:
        return jsonify({"error": "field 'content_id' is required"}), 400
    if not reasoning:
        return jsonify({"error": "field 'creator_reasoning' is required"}), 400

    row = audit_log.record_appeal(content_id=content_id, reason=reasoning)
    if row is None:
        return jsonify({"error": "unknown content_id"}), 404

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "appeal_logged": True,
        "message": "Appeal received. This submission is now under review by a human.",
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
