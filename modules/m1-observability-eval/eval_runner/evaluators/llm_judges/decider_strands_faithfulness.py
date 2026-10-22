"""Strands Decider counterpart to FaithfulnessJudge. Writes to "faithfulness__strands"."""

from evaluators.base import EvalResult
from evaluators.llm_judges.decider_strands_base import StrandsDeciderBase

_CRITERIA = [
    "directly contradicts context or contains major unsupported assertions",
    "several claims not supported by the context",
    "mostly faithful with minor unsupported details",
    "fully faithful with no contradictions or unsupported claims",
]

_INSTRUCTIONS = "Rate how faithful this agent response is to the provided context."


class DeciderStrandsFaithfulnessJudge(StrandsDeciderBase):
    name = "faithfulness_judge_strands"
    metric = "faithfulness__strands"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_output = attrs.get("task.output", "")
        task_input = attrs.get("task.input", "")

        llm_context = ""
        llm_spans: list[dict] = context.get("llm_spans", [])
        if llm_spans:
            first_llm = llm_spans[0]
            llm_attrs = first_llm.get("attributes", {})
            llm_context = llm_attrs.get("gen_ai.prompt.0.content", "")
        if not llm_context:
            llm_context = task_input

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate faithfulness",
                eval_type=self.eval_type,
            )

        state = (
            f"Context: {llm_context[:3000]}\n\n"
            f"Input: {task_input or '(no explicit input provided)'}\n\n"
            f"Agent response to evaluate: {task_output}"
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
