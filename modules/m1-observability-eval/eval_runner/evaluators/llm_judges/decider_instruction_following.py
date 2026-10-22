"""
Decider-backed shadow counterpart to InstructionFollowingJudge (see
instruction_following.py).

Writes under "instruction_following__decider" — see decider_bias.py's module
docstring for the general pattern this and the other Group 1 decider judges
follow. Mirrors the incumbent's "no instructions found" edge case exactly.
"""

from evaluators.base import EvalResult
from evaluators.llm_judges.decider_base import DeciderBase

_CRITERIA = [
    "instructions are largely ignored or violated",
    "many instructions missed or violated",
    "most instructions followed, with some gaps",
    "all instructions are followed precisely",
]

_INSTRUCTIONS = "Rate how well this agent response follows the explicit instructions given."


class DeciderInstructionFollowingJudge(DeciderBase):
    name = "instruction_following_judge_decider"
    metric = "instruction_following__decider"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_output = attrs.get("task.output", "")

        instructions = ""
        llm_spans: list[dict] = context.get("llm_spans", [])
        if llm_spans:
            first_llm = llm_spans[0]
            llm_attrs = first_llm.get("attributes", {})
            instructions = llm_attrs.get("gen_ai.prompt.0.content", "")
        if not instructions:
            instructions = attrs.get("task.input", "")

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate instruction following",
                eval_type=self.eval_type,
            )

        if not instructions:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.5,
                reasoning=(
                    "No instructions found in LLM prompt or task.input; "
                    "instruction following cannot be evaluated"
                ),
                eval_type=self.eval_type,
            )

        state = f"Instructions: {instructions[:2000]}\n\nAgent response to evaluate: {task_output[:2000]}"

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
