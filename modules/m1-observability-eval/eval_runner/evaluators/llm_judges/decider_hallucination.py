"""
Decider-backed shadow counterpart to HallucinationJudge (see hallucination.py).

Writes under "hallucination_score__decider" — see decider_bias.py's module
docstring for the general pattern this and the other Group 1 decider judges
follow. Builds the same grounding context (tool outputs + LLM messages) as
the incumbent judge.
"""

from evaluators.base import EvalResult
from evaluators.llm_judges.decider_base import DeciderBase

_CRITERIA = [
    "severe: clearly invented facts, e.g. wrong names, fabricated statistics",
    "moderate: multiple unverifiable specific claims stated as fact",
    "minor risk: one or two unverifiable claims, but plausible",
    "no hallucination: all factual claims grounded, hedged, or none present",
]

_INSTRUCTIONS = (
    "Rate the hallucination risk in this agent response: does it state specific "
    "factual claims (names, numbers, dates, URLs, statistics) as definitive fact "
    "that cannot be verified from the observable grounding context? This is not "
    "about helpfulness or completeness — only whether concrete factual claims "
    "are invented vs. grounded."
)


class DeciderHallucinationJudge(DeciderBase):
    name = "hallucination_judge_decider"
    metric = "hallucination_score__decider"

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

        grounding_fragments = []
        for ts in (context.get("tool_spans") or []):
            ta = ts.get("attributes", {}) or {}
            for key in ("tool.output", "output", "tool.result"):
                val = ta.get(key, "")
                if val:
                    grounding_fragments.append(f"[tool output] {str(val)[:400]}")
                    break
        for ls in (context.get("llm_spans") or []):
            la = ls.get("attributes", {}) or {}
            for key in ("gen_ai.completion", "llm.output", "output"):
                val = la.get(key, "")
                if val:
                    grounding_fragments.append(f"[llm message] {str(val)[:400]}")
                    break
        grounding = "\n".join(grounding_fragments[:10]) if grounding_fragments else "(no grounding context captured)"

        state = (
            f"User input: {task_input[:600]}\n\n"
            f"Observable grounding context (tool outputs, retrieved data): {grounding}\n\n"
            f"Agent response to evaluate: {task_output[:800]}"
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
