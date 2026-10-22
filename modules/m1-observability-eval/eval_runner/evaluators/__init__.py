from .deterministic.format_check import FormatComplianceEvaluator
from .deterministic.tool_accuracy import ToolAccuracyEvaluator
from .deterministic.tool_selection import ToolSelectionEvaluator
from .deterministic.tool_argument_accuracy import ToolArgumentAccuracyEvaluator
from .deterministic.step_efficiency import StepEfficiencyEvaluator
from .deterministic.task_success import TaskSuccessEvaluator
from .deterministic.agent_failure_rate import AgentFailureRateEvaluator
from .deterministic.tool_error_rate import ToolErrorRateEvaluator
from .deterministic.tool_retry_rate import ToolRetryRateEvaluator
from .deterministic.handoff_success_rate import HandoffSuccessRateEvaluator
from .deterministic.timeout_rate import TimeoutRateEvaluator
from .deterministic.error_recovery_rate import ErrorRecoveryRateEvaluator
from .deterministic.context_propagation import ContextPropagationEvaluator
from .deterministic.trace_completeness import TraceCompletenessEvaluator
from .deterministic.dead_span_rate import DeadSpanRateEvaluator
from .llm_judges.faithfulness import FaithfulnessJudge
from .llm_judges.relevance import RelevanceJudge
from .llm_judges.instruction_following import InstructionFollowingJudge
from .llm_judges.handoff_fidelity import HandoffFidelityJudge
from .llm_judges.qa_correctness import QACorrectnessJudge
from .llm_judges.custom_rubric import CustomRubricJudge
from .llm_judges.knowledge_retention import KnowledgeRetentionJudge
from .llm_judges.role_adherence import RoleAdherenceJudge
from .llm_judges.conversation_completeness import ConversationCompletenessJudge
from .llm_judges.hallucination import HallucinationJudge
from .llm_judges.toxicity import ToxicityJudge
from .llm_judges.bias import BiasJudge
from .llm_judges.coherence import CoherenceJudge
from .llm_judges.conciseness import ConcisenessJudge
from .llm_judges.conversation_relevancy import ConversationRelevancyJudge
from .deterministic.tool_output_handling import ToolOutputHandlingEvaluator
from .llm_judges.decider_bias import DeciderBiasJudge
from .llm_judges.decider_toxicity import DeciderToxicityJudge
from .llm_judges.decider_coherence import DeciderCoherenceJudge
from .llm_judges.decider_conciseness import DeciderConcisenessJudge
from .llm_judges.decider_relevance import DeciderRelevanceJudge
from .llm_judges.decider_faithfulness import DeciderFaithfulnessJudge
from .llm_judges.decider_hallucination import DeciderHallucinationJudge
from .llm_judges.decider_instruction_following import DeciderInstructionFollowingJudge
from .llm_judges.decider_strands_bias import DeciderStrandsBiasJudge
from .llm_judges.decider_strands_toxicity import DeciderStrandsToxicityJudge
from .llm_judges.decider_strands_coherence import DeciderStrandsCoherenceJudge
from .llm_judges.decider_strands_conciseness import DeciderStrandsConcisenessJudge
from .llm_judges.decider_strands_relevance import DeciderStrandsRelevanceJudge
from .llm_judges.decider_strands_faithfulness import DeciderStrandsFaithfulnessJudge
from .llm_judges.decider_strands_hallucination import DeciderStrandsHallucinationJudge
from .llm_judges.decider_strands_instruction_following import DeciderStrandsInstructionFollowingJudge

ALL_EVALUATORS = [
    FormatComplianceEvaluator(),
    ToolAccuracyEvaluator(),
    ToolSelectionEvaluator(),
    ToolArgumentAccuracyEvaluator(),
    StepEfficiencyEvaluator(),
    TaskSuccessEvaluator(),
    AgentFailureRateEvaluator(),
    ToolErrorRateEvaluator(),
    ToolRetryRateEvaluator(),
    HandoffSuccessRateEvaluator(),
    TimeoutRateEvaluator(),
    ErrorRecoveryRateEvaluator(),
    ContextPropagationEvaluator(),
    TraceCompletenessEvaluator(),
    DeadSpanRateEvaluator(),
    FaithfulnessJudge(),
    RelevanceJudge(),
    InstructionFollowingJudge(),
    HandoffFidelityJudge(),
    QACorrectnessJudge(),
    CustomRubricJudge(),
    KnowledgeRetentionJudge(),
    RoleAdherenceJudge(),
    ConversationCompletenessJudge(),
    HallucinationJudge(),
    ToxicityJudge(),
    BiasJudge(),
    CoherenceJudge(),
    ConcisenessJudge(),
    ConversationRelevancyJudge(),
    ToolOutputHandlingEvaluator(),
    # Dormant unless the pipeline explicitly requests them by name (see
    # eval_pipeline.py's _DECIDER_COUNTERPART, gated by DECIDER_ENABLED
    # / DECIDER_METRICS).
    DeciderBiasJudge(),
    DeciderToxicityJudge(),
    DeciderCoherenceJudge(),
    DeciderConcisenessJudge(),
    DeciderRelevanceJudge(),
    DeciderFaithfulnessJudge(),
    DeciderHallucinationJudge(),
    DeciderInstructionFollowingJudge(),
    # Second, independent decision-model track (Strands Decider, not Ollama) —
    # also dormant unless the pipeline explicitly requests them by name, gated
    # by STRANDS_DECIDER_ENABLED / STRANDS_DECIDER_METRICS.
    DeciderStrandsBiasJudge(),
    DeciderStrandsToxicityJudge(),
    DeciderStrandsCoherenceJudge(),
    DeciderStrandsConcisenessJudge(),
    DeciderStrandsRelevanceJudge(),
    DeciderStrandsFaithfulnessJudge(),
    DeciderStrandsHallucinationJudge(),
    DeciderStrandsInstructionFollowingJudge(),
]

EVALUATOR_MAP = {e.name: e for e in ALL_EVALUATORS}

__all__ = [
    "FormatComplianceEvaluator",
    "ToolAccuracyEvaluator",
    "ToolSelectionEvaluator",
    "ToolArgumentAccuracyEvaluator",
    "StepEfficiencyEvaluator",
    "TaskSuccessEvaluator",
    "AgentFailureRateEvaluator",
    "ToolErrorRateEvaluator",
    "ToolRetryRateEvaluator",
    "HandoffSuccessRateEvaluator",
    "TimeoutRateEvaluator",
    "ErrorRecoveryRateEvaluator",
    "ContextPropagationEvaluator",
    "TraceCompletenessEvaluator",
    "DeadSpanRateEvaluator",
    "FaithfulnessJudge",
    "RelevanceJudge",
    "InstructionFollowingJudge",
    "HandoffFidelityJudge",
    "QACorrectnessJudge",
    "CustomRubricJudge",
    "KnowledgeRetentionJudge",
    "RoleAdherenceJudge",
    "ConversationCompletenessJudge",
    "HallucinationJudge",
    "ToxicityJudge",
    "BiasJudge",
    "CoherenceJudge",
    "ConcisenessJudge",
    "ConversationRelevancyJudge",
    "ToolOutputHandlingEvaluator",
    "DeciderBiasJudge",
    "DeciderToxicityJudge",
    "DeciderCoherenceJudge",
    "DeciderConcisenessJudge",
    "DeciderRelevanceJudge",
    "DeciderFaithfulnessJudge",
    "DeciderHallucinationJudge",
    "DeciderInstructionFollowingJudge",
    "DeciderStrandsBiasJudge",
    "DeciderStrandsToxicityJudge",
    "DeciderStrandsCoherenceJudge",
    "DeciderStrandsConcisenessJudge",
    "DeciderStrandsRelevanceJudge",
    "DeciderStrandsFaithfulnessJudge",
    "DeciderStrandsHallucinationJudge",
    "DeciderStrandsInstructionFollowingJudge",
    "ALL_EVALUATORS",
    "EVALUATOR_MAP",
]
