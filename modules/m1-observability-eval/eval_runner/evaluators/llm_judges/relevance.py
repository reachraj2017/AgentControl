"""
Relevance judge.
Evaluates whether the agent output is relevant to the task input.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class RelevanceJudge(JudgeBase):
    """Scores how on-topic the output is relative to the task input."""

    name = "relevance_judge"
    metric = "relevance"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_input = attrs.get("task.input", "")
        task_output = attrs.get("task.output", "")

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate relevance",
                eval_type=self.eval_type,
            )

        if not task_input:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.5,
                reasoning="task.input is missing; relevance cannot be determined",
                eval_type=self.eval_type,
            )

        criteria = (
            "Rate how relevant the output is to the input question or task. "
            "1.0 = output directly and completely addresses the input. "
            "0.0 = output is entirely off-topic or ignores the input."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=task_input[:2000],
            output_text=task_output[:2000],
        )

        try:
            score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms = self._call_judge(prompt)
        except Exception as exc:
            log.error("relevance_judge_failed", error=str(exc))
            score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms = 0.5, f"Judge call failed: {exc}", 0, 0, 0.0, 0.0

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
