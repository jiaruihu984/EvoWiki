"""Integration backend for the personal wiki: replayable wiki events, state, and the flows behind
the /wiki API. Event bodies are snapshots (D1): replay never calls an LLM or re-runs guardrails."""
