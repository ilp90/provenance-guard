"""Confidence scoring — combine the two signals into one calibrated verdict.

Implements planning.md §2.2 (combination) and §5 (thresholds, disagreement
override, confidence formula). Kept dependency-free and pure so it can be unit
tested without touching Flask or Groq.
"""

# §2.2 — combination weights (LLM weighted higher: semantic > structural).
W_LLM = 0.60
W_STYLOMETRY = 0.40

# §5 — asymmetric verdict thresholds on combined_ai_probability.
# High bar to accuse AI (>=0.70); generous human band (<=0.35).
HUMAN_MAX = 0.35
AI_MIN = 0.70

# §5 — signals this far apart force an honest 'uncertain'.
DISAGREEMENT_LIMIT = 0.40

# §6 — a label is only "high-confidence" at or above this.
HIGH_CONFIDENCE_MIN = 0.50


def _clamp01(x):
    return max(0.0, min(1.0, x))


def score(llm_ai_prob, stylometry_ai_prob):
    """Combine two AI-probabilities into a verdict + confidence.

    Returns a dict:
      combined_ai_probability, disagreement, verdict,
      confidence, high_confidence (bool)
    """
    combined = W_LLM * llm_ai_prob + W_STYLOMETRY * stylometry_ai_prob
    disagreement = abs(llm_ai_prob - stylometry_ai_prob)

    # Verdict from the combined score, then the disagreement override.
    if combined >= AI_MIN:
        verdict = "likely_ai"
    elif combined <= HUMAN_MAX:
        verdict = "likely_human"
    else:
        verdict = "uncertain"

    if disagreement >= DISAGREEMENT_LIMIT:
        # The signals tell two different stories — refuse to pick one.
        verdict = "uncertain"

    # Decisiveness minus the disagreement penalty (§5).
    confidence = _clamp01(abs(combined - 0.5) * 2 - disagreement)

    return {
        "combined_ai_probability": round(combined, 4),
        "disagreement": round(disagreement, 4),
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "high_confidence": confidence >= HIGH_CONFIDENCE_MIN,
    }
