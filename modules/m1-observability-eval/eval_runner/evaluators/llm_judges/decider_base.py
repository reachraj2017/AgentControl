"""
Base class for decision-model-backed evaluators.

Calls a local Ollama instance (>= 0.35) serving one of its decision models
("nimble" or "tev1") via its `/v1/systemone` API — typed score/noul/choice
questions, calibrated confidence, no generation loop. No JSON-parsing
fallback needed the way JudgeBase's _call_judge has to handle for a
conversational LLM's reply.

Controlled by:
  DECIDER_ENABLED   "true" to allow any decider evaluator to run (default: false)
  DECIDER_BASE_URL  base URL of the Ollama instance (default: http://localhost:11434;
                     use http://host.docker.internal:11434 to reach a native
                     host process from inside a container)
  DECIDER_MODEL     which Ollama decision model to call ("nimble" or "tev1";
                     default: "nimble")
  DECIDER_METRICS   comma-separated allowlist of base (non-suffixed) metric
                     names this is permitted to run for, e.g. "bias_score"

A decider evaluator never degrades the incumbent judge it's shadowing: any
failure (disabled, unreachable, bad response) propagates as an exception,
which the pipeline's existing per-evaluator try/except skips — writing no
row at all, rather than a guessed fallback score.
"""

import os
import time

import httpx

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "nimble"


def decider_enabled_for(base_metric: str) -> bool:
    """True if the decider should run for this base (non-suffixed) metric name."""
    if os.getenv("DECIDER_ENABLED", "false").strip().lower() != "true":
        return False
    allowlist = {
        m.strip() for m in os.getenv("DECIDER_METRICS", "").split(",") if m.strip()
    }
    return base_metric in allowlist


class DeciderBase:
    """Shared HTTP infrastructure for decision-model-backed evaluators (Ollama)."""

    eval_type = "llm_judge_decider"

    @property
    def _base_url(self) -> str:
        return os.getenv("DECIDER_BASE_URL", _DEFAULT_BASE_URL)

    @property
    def _model(self) -> str:
        return os.getenv("DECIDER_MODEL", _DEFAULT_MODEL)

    def _call_decider_score(
        self,
        state: str,
        question_key: str,
        instructions: str,
        criteria: list[str],
        timeout: float = 15.0,
    ) -> tuple[float, str, int, int, float, float]:
        """
        Ask a single `score` question and normalise the result to [0.0, 1.0].

        `criteria` must be ascending (index 0 = low end), per Ollama's
        ScoreQuestion contract — the returned raw score is a float over
        [0, len(criteria)-1], which this normalises by dividing by
        len(criteria)-1.

        cost_usd is always 0.0 — Ollama is self-hosted, with no metered
        per-token billing. That's a real finding, not a missing data point.

        Returns:
            (normalised_score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms)
        """
        model = self._model
        payload = {
            "model": model,
            "state": state,
            "questions": {
                question_key: {
                    "type": "score",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }

        start = time.monotonic()
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{self._base_url}/v1/systemone", json=payload)
            resp.raise_for_status()
            data = resp.json()
        elapsed_ms = (time.monotonic() - start) * 1000

        answer = data["answers"][question_key]
        raw_score = float(answer["score"])  # 0 .. len(criteria)-1 — a probability-weighted
                                             # expectation over the bins below, not a raw
                                             # pre-softmax logit.
        confidence = float(answer.get("confidence", 0.0))
        normalised = raw_score / (len(criteria) - 1) if len(criteria) > 1 else raw_score

        usage = data.get("usage", {}) or {}
        tokens_in = int(usage.get("input_tokens", 0) or 0)
        tokens_out = int(usage.get("output_tokens", 0) or 0)
        cost_usd = 0.0

        legend = answer.get("legend", {}) or {}
        # Full softmax distribution over the bins, keyed by label rather than index —
        # the model's actual output; raw_score/confidence above are scalar summaries of it.
        probabilities = {
            legend.get(k, k): round(float(v), 4)
            for k, v in (answer.get("probabilities", {}) or {}).items()
        }

        reasoning = (
            f"model={data.get('model', model)} raw_score={raw_score:.3f} "
            f"confidence={confidence:.3f} probabilities={probabilities} "
            f"latency_ms={elapsed_ms:.2f} legend={legend}"
        )
        return normalised, reasoning, tokens_in, tokens_out, cost_usd, elapsed_ms
