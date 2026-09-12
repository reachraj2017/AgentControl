"""Shadow eval pipeline — scores shadow calls stored in gateway_call_log.

Runs as a periodic background task in the eval runner. Each cycle:
  1. Fetches unscored shadow rows from gateway_call_log (is_shadow=1)
  2. Runs faithfulness, relevance, and instruction_following LLM judges
  3. Writes results to gateway_shadow_evals
"""

import json
import logging
import os

log = logging.getLogger("shadow_eval")

_JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4o-mini")

_JUDGE_PROMPT = (
    "You are an expert evaluator. Rate the following AI response on ONE metric.\n\n"
    "Metric: {metric}\n"
    "Criteria: {criteria}\n\n"
    "Prompt:\n{prompt}\n\n"
    "Response:\n{response}\n\n"
    'Output ONLY a JSON object like: {{"score": 0.85, "reasoning": "brief explanation"}}\n'
    "Score must be between 0.0 and 1.0."
)

_METRICS = [
    (
        "faithfulness",
        "Rate how faithful and accurate the response is. "
        "1.0 = fully accurate, 0.0 = hallucinates or contradicts the prompt.",
    ),
    (
        "relevance",
        "Rate how relevant and useful the response is to answering the prompt. "
        "1.0 = directly answers, 0.0 = off-topic.",
    ),
    (
        "instruction_following",
        "Rate how well the response follows explicit instructions in the prompt. "
        "1.0 = follows all instructions, 0.0 = ignores them.",
    ),
]


_SHADOW_EVALS_DDL = """
    CREATE TABLE IF NOT EXISTS otel.gateway_shadow_evals (
        shadow_eval_id  String               DEFAULT generateUUIDv4(),
        call_id         String               DEFAULT '',
        agent_role      String               DEFAULT '',
        model_used      String               DEFAULT '',
        prompt_text     String               DEFAULT '',
        response_text   String               DEFAULT '',
        scores          Map(String, Float32) DEFAULT map(),
        scored_at       DateTime64(3)        DEFAULT now64(3)
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(scored_at)
    ORDER BY (agent_role, scored_at)
"""


class ShadowEvalPipeline:
    """Scores unscored shadow calls using LLM judges."""

    def __init__(self, repository) -> None:
        self._repo = repository
        self._ensure_table()

    def _ensure_table(self) -> None:
        try:
            self._repo._ch.execute(_SHADOW_EVALS_DDL)
        except Exception as exc:
            log.warning("ensure_shadow_evals_table failed: %s", exc)

    def _score_one(self, prompt_text: str, response_text: str) -> dict[str, float]:
        import litellm

        scores: dict[str, float] = {}
        for metric, criteria in _METRICS:
            try:
                msg = _JUDGE_PROMPT.format(
                    metric=metric,
                    criteria=criteria,
                    prompt=prompt_text[:2000],
                    response=response_text[:2000],
                )
                resp = litellm.completion(
                    model=_JUDGE_MODEL,
                    messages=[{"role": "user", "content": msg}],
                    max_tokens=150,
                    temperature=0,
                )
                raw = (resp.choices[0].message.content or "").strip()
                start = raw.find("{")
                end   = raw.rfind("}") + 1
                if start >= 0 and end > start:
                    data  = json.loads(raw[start:end])
                    score = float(data.get("score", 0.0))
                    scores[metric] = max(0.0, min(1.0, score))
            except Exception as exc:
                log.debug("shadow judge failed metric=%s: %s", metric, exc)
        return scores

    def run_batch(self, batch_size: int = 20) -> int:
        """Score one batch of unscored shadow rows. Returns count scored."""
        rows = self._repo.get_unscored_shadow_rows(batch_size)
        scored = 0
        for row in rows:
            call_id       = row.get("call_id", "")
            prompt_text   = row.get("prompt_text", "")
            response_text = row.get("response_text", "")
            if not call_id or not prompt_text or not response_text:
                continue
            scores = self._score_one(prompt_text, response_text)
            if scores:
                self._repo.save_shadow_eval_scores(
                    call_id=call_id,
                    agent_role=row.get("agent_role", ""),
                    model_used=row.get("model_used", ""),
                    prompt_text=prompt_text,
                    response_text=response_text,
                    scores=scores,
                )
                scored += 1
        return scored
