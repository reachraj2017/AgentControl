"""
Base class for LLM-as-judge evaluators.

Model is controlled by the JUDGE_MODEL env var (default: anthropic/claude-haiku-4-5-20251001).
Supports any provider via LiteLLM:
  anthropic/claude-haiku-4-5-20251001   — default, Claude via Anthropic API
  openai/gpt-4o-mini                    — OpenAI
  ollama/gemma4:26b                     — local Ollama (set OLLAMA_BASE_URL if not localhost)
"""

import json
import os
import time
from typing import Optional

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from evaluators.base import BaseEvaluator

log = structlog.get_logger(__name__)

_DEFAULT_JUDGE_MODEL = "anthropic/claude-haiku-4-5-20251001"

_JUDGE_SYSTEM = (
    "You are an expert AI evaluator. "
    "Score outputs on a scale of 0.0 to 1.0. "
    'Always respond with valid JSON: {"score": <float>, "reasoning": <string>}'
)


def _litellm_kwargs(model: str) -> dict:
    """Return extra kwargs needed for the given model provider."""
    kwargs: dict = {}
    if "ollama" in model.lower():
        kwargs["api_base"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        kwargs["extra_body"] = {"think": False}
    return kwargs


class JudgeBase(BaseEvaluator):
    """Shared infrastructure for LLM-as-judge evaluators."""

    eval_type = "llm_judge"

    @property
    def _judge_model(self) -> str:
        return os.getenv("JUDGE_MODEL", _DEFAULT_JUDGE_MODEL)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=False,
    )
    def _call_judge(self, prompt: str) -> tuple[float, str, int, int, float, float]:
        """
        Call the configured judge model with a scoring prompt.

        Tokens/cost/latency are returned, not stashed on `self` — judge
        instances are singletons shared across every evaluate() call, and
        concurrent traces run pipeline.run() in separate threads
        (asyncio.to_thread), so instance attributes here would race.

        Returns:
            (score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms)
        """
        import litellm

        model = self._judge_model
        try:
            start = time.monotonic()
            response = litellm.completion(
                model=model,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                **_litellm_kwargs(model),
            )
            latency_ms = (time.monotonic() - start) * 1000

            usage = getattr(response, "usage", None)
            tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
            tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
            try:
                cost_usd = float(litellm.completion_cost(completion_response=response) or 0.0)
            except Exception:
                # Self-hosted/local models (e.g. ollama/...) have no metered
                # price in litellm's table — 0.0 is the correct answer, not
                # a failure.
                cost_usd = 0.0

            raw_text = (response.choices[0].message.content or "").strip()

            # Strip markdown code fences if present
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            parsed = json.loads(raw_text)
            score = self.clamp_score(float(parsed.get("score", 0.5)))
            reasoning = str(parsed.get("reasoning", ""))
            log.debug("judge_scored", model=model, score=score)
            return score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms

        except json.JSONDecodeError as exc:
            log.warning("judge_json_parse_error", error=str(exc))
            # The API call itself succeeded — tokens/cost/latency are real,
            # only the response's content failed to parse as JSON.
            return (
                0.5, "Parse error: could not decode judge response as JSON",
                tokens_in, tokens_out, cost_usd, latency_ms,
            )
        except Exception as exc:
            log.error("judge_call_failed", model=model, error=str(exc))
            raise  # Let tenacity retry

    def _build_prompt(
        self,
        criteria: str,
        input_text: str,
        output_text: str,
        context_text: str = "",
    ) -> str:
        """Build a structured evaluation prompt."""
        parts = [f"## Evaluation Criteria\n{criteria}\n"]

        if context_text:
            parts.append(f"## Context\n{context_text}\n")

        parts.append(f"## Input / Task\n{input_text}\n")
        parts.append(f"## Output to Evaluate\n{output_text}\n")
        parts.append(
            '## Instructions\nRespond ONLY with valid JSON: {"score": <0.0-1.0>, "reasoning": "<brief explanation>"}'
        )

        return "\n".join(parts)
