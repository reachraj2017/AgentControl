"""
Decider-backed shadow counterpart to CoherenceJudge (see coherence.py).

Writes under "coherence__decider" — see decider_bias.py's module docstring
for the general pattern this and the other Group 1 decider judges follow.
"""

from evaluators.base import EvalResult
from evaluators.llm_judges.decider_base import DeciderBase

_CRITERIA = [
    "completely incoherent or unreadable",
    "significant problems: ideas jump without connection, frequent errors",
    "moderate issue: one contradictory statement or several grammar errors",
    "excellent: logical flow, internally consistent, fluent prose",
]

_INSTRUCTIONS = (
    "Rate this agent response on logical flow (ideas progress in a natural, "
    "ordered sequence, no non-sequiturs), internal consistency (no "
    "self-contradictions), and fluency (grammatically correct, readable, no "
    "garbled or truncated text), combined into one score."
)


class DeciderCoherenceJudge(DeciderBase):
    name = "coherence_judge_decider"
    metric = "coherence__decider"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {}) or {}
        task_output = attrs.get("task.output", attrs.get("output", ""))
        task_input  = attrs.get("task.input",  attrs.get("input", ""))

        if not task_output or not task_output.strip():
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No output to evaluate.",
                eval_type=self.eval_type,
            )

        state = (
            f"Task input: {task_input or '(no explicit input provided)'}\n\n"
            f"Agent response to evaluate: {task_output[:1500]}"
        )

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
