"""
Decider-backed shadow counterpart to RelevanceJudge (see relevance.py).

Writes under "relevance__decider" — see decider_bias.py's module docstring
for the general pattern this and the other Group 1 decider judges follow.
Mirrors the incumbent's empty-input/output edge cases exactly, so a call
that produces a degenerate score for one judge produces the same for both.
"""

from evaluators.base import EvalResult
from evaluators.llm_judges.decider_base import DeciderBase

_CRITERIA = [
    "entirely off-topic or ignores the input",
    "mostly off-topic with minor relevance",
    "partially relevant but missing key aspects of the input",
    "directly and completely addresses the input",
]

_INSTRUCTIONS = "Rate how relevant this agent response is to the input question or task."


class DeciderRelevanceJudge(DeciderBase):
    name = "relevance_judge_decider"
    metric = "relevance__decider"

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

        state = f"Input: {task_input[:2000]}\n\nAgent response to evaluate: {task_output[:2000]}"

        score, reasoning, tokens_in, tokens_out, cost_usd, latency_ms = self._call_decider_score(
            state=state,
            question_key=self.metric,
            instructions=_INSTRUCTIONS,
            criteria=_CRITERIA,
        )

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
