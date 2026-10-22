"""
Base class for Strands Decider-backed evaluators — a second, independent
decision-model track alongside the existing Ollama-backed one
(decider_base.py). Deliberately separate code, not a shared/switchable
backend: this lets both run side by side without any risk to the working
Ollama path. Writes to `<metric>__strands`-suffixed metrics, distinct from
Ollama's `<metric>__decider` suffix, so neither ever collides with the
other or with the incumbent judge's own metric.

Calls a local `strands-decider serve ...` process (native host process,
not Docker-managed) via its `/v1/systemone` API — same typed score/noul/
choice contract Ollama's decision models use, but served by its own model
pinned at `serve` time, so no "model" field is sent in the request body
(unlike the Ollama track, which must specify one per call).

Controlled by:
  STRANDS_DECIDER_ENABLED   "true" to allow any Strands evaluator to run (default: false)
  STRANDS_DECIDER_BASE_URL  base URL of the running `strands-decider serve` instance
                            (default: http://localhost:8000; use
                            http://host.docker.internal:8000 to reach a native
                            host process from inside a container)
  STRANDS_DECIDER_METRICS   comma-separated allowlist of base (non-suffixed)
                            metric names this is permitted to run for, e.g. "bias_score"

Never degrades the incumbent judge or the Ollama decider it's shadowing
alongside: any failure (disabled, unreachable, bad response) propagates as
an exception, which the pipeline's existing per-evaluator try/except skips
— writing no row at all, rather than a guessed fallback score.
"""

import os
import time

import httpx

_DEFAULT_BASE_URL = "http://localhost:8000"


def strands_decider_enabled_for(base_metric: str) -> bool:
    """True if the Strands Decider track should run for this base metric name."""
    if os.getenv("STRANDS_DECIDER_ENABLED", "false").strip().lower() != "true":
        return False
    allowlist = {
        m.strip() for m in os.getenv("STRANDS_DECIDER_METRICS", "").split(",") if m.strip()
    }
    return base_metric in allowlist


class StrandsDeciderBase:
    """Shared HTTP infrastructure for Strands Decider-backed evaluators."""

    eval_type = "llm_judge_decider_strands"

    @property
    def _base_url(self) -> str:
        return os.getenv("STRANDS_DECIDER_BASE_URL", _DEFAULT_BASE_URL)

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

        `criteria` must be ascending (index 0 = low end). cost_usd is always
        0.0 — self-hosted, no metered billing.

        Returns:
            (normalised_score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms)
        """
        payload = {
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
        raw_score = float(answer["score"])  # 0 .. len(criteria)-1
        confidence = float(answer.get("confidence", 0.0))
        normalised = raw_score / (len(criteria) - 1) if len(criteria) > 1 else raw_score

        usage = data.get("usage", {}) or {}
        tokens_in = int(usage.get("input_tokens", 0) or 0)
        tokens_out = int(usage.get("output_tokens", 0) or 0)
        cost_usd = 0.0

        legend = answer.get("legend", {}) or {}
        probabilities = {
            legend.get(k, k): round(float(v), 4)
            for k, v in (answer.get("probabilities", {}) or {}).items()
        }

        reasoning = (
            f"model={data.get('model', 'strands-decider')} raw_score={raw_score:.3f} "
            f"confidence={confidence:.3f} probabilities={probabilities} "
            f"latency_ms={elapsed_ms:.2f} legend={legend}"
        )
        return normalised, reasoning, tokens_in, tokens_out, cost_usd, elapsed_ms
