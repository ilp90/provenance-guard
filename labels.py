"""Transparency labels — the reader-facing text.

Three variants (planning.md §6), selected by (verdict, confidence). The rule is
deliberately asymmetric: an AI or human label is only shown at high confidence;
anything else falls back to the non-accusatory 'uncertain' label. This is the
false-positive backstop from §3 — a low-confidence 'likely_ai' verdict never
reaches the reader as an accusation.
"""
from scoring import HIGH_CONFIDENCE_MIN

# Verbatim label text — the single source of truth, mirrored in the README.
LABELS = {
    "high_confidence_human": (
        "✅ Likely written by a human. Our automated checks found strong signs "
        "this was written by a person, and our two independent checks agree. "
        "This is an estimate, not a guarantee."
    ),
    "high_confidence_ai": (
        "🤖 Likely AI-generated. Our automated checks found strong signs this "
        "text was produced with an AI tool. This is an automated estimate, not "
        "a certainty — if you wrote this yourself, you can appeal and a human "
        "will review it."
    ),
    "uncertain": (
        "❔ Not enough signal to tell. Our checks couldn't confidently determine "
        "whether this was written by a person or an AI. Please treat this as "
        "undetermined — it is not a judgment that the work is AI-generated."
    ),
}


def build_label(verdict, confidence):
    """Map (verdict, confidence) -> {'variant': ..., 'text': ...}.

    - likely_human + confidence >= 0.50 -> high-confidence human
    - likely_ai    + confidence >= 0.50 -> high-confidence AI
    - everything else                   -> uncertain
    """
    high = confidence >= HIGH_CONFIDENCE_MIN
    if verdict == "likely_human" and high:
        variant = "high_confidence_human"
    elif verdict == "likely_ai" and high:
        variant = "high_confidence_ai"
    else:
        variant = "uncertain"
    return {"variant": variant, "text": LABELS[variant]}
