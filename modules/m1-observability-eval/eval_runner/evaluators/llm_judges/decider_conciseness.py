"""
Decider-backed shadow counterpart to ConcisenessJudge (see conciseness.py).

Writes under "conciseness__decider" — see decider_bias.py's module docstring
for the general pattern this and the other Group 1 decider judges follow.
"""

from evaluators.base import EvalResult
from evaluators.llm_judges.decider_base import DeciderBase

_CRITERIA = [
    "almost entirely padding or repetition with minimal useful content",
    "significantly bloated: core answer buried in filler",
    "moderate padding or repetition, roughly 20-30% filler",
    "perfectly concise: every sentence adds value",
]

_INSTRUCTIONS = (
    "Rate how appropriately concise this agent response is for the task. Penalise "
    "padding (filler phrases like 'Great question!'), repetition (restating the "
    "same point multiple times), over-qualification (excessive caveats beyond "
    "what the task warrants), off-topic content, and verbosity. Do not penalise "
    "length that is genuinely necessary to answer the task fully."
)


class DeciderConcisenessJudge(DeciderBase):
    name = "conciseness_judge_decider"
    metric = "conciseness__decider"

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
