"""
metrics_judge.py — LLM-as-judge scoring: faithfulness, answer relevance,
correctness, context relevance.

Standard enterprise-RAG practice (RAGAS/TruLens/DeepEval/Azure AI Eval SDK
all implement variants of this). Pluggable by design — same pattern as
embed_fn in metrics_generation.py — so this framework never hard-depends
on Azure OpenAI or any specific judge model. If no judge_fn is supplied,
these scores are simply omitted (not zeroed, not faked).

WHY THESE FOUR METRICS
  - faithfulness       : does the answer only state things supported by
                          the retrieved context? (catches hallucination
                          even when retrieval was correct — a gap none
                          of the lexical/embedding metrics can see)
  - answer_relevance   : does the answer actually address the question
                          asked (independent of whether it matches the
                          expected_answer wording)
  - correctness        : does the answer match expected_answer in
                          substance (the LLM-judged upgrade over
                          token_f1/sequence_similarity — handles
                          paraphrase, valid alternate phrasing, etc.)
  - context_relevance  : is the retrieved_context actually relevant to
                          the question (a retrieval-quality signal
                          independent of chunk_id matching — catches
                          "right id, but is this content even useful"
                          cases that id-matching alone can't)

THE PLUGGABLE CONTRACT
  def my_judge_fn(question: str, expected_answer: str, actual_answer: str,
                   retrieved_context: list[str]) -> dict:
      # call whatever LLM you like, in whatever prompt style you like
      return {
          "faithfulness": 0.9,       # 0.0-1.0, or None if context wasn't supplied
          "answer_relevance": 0.8,   # 0.0-1.0
          "correctness": 0.7,        # 0.0-1.0
          "context_relevance": 0.85, # 0.0-1.0, or None if context wasn't supplied
      }

All four keys are optional in the returned dict — omit any the judge
doesn't score. Framework aggregates whatever is present.

CHANGE LOG
v1.1.0 — Aug 2026 | Mukesh Kund
         Fix: example_judge_fn_using_your_llm's JSON parser replaced —
         naive strip/remove-backticks approach failed on real gpt-5-nano
         responses ("JSONDecodeError: Expecting value: line 1 column 1").
         Replaced with the same brace-position extraction (find "{" /
         rfind "}") already established and working in classifier_node
         .py's classify_intent() for the identical GPT-5-reasoning-model
         prose-prepended-JSON problem — should have referenced that
         existing fix from the start instead of guessing a token-budget
         explanation first.
         ROLLBACK: revert to raw_text.strip().removeprefix(...) chain —
         will reproduce the parse failure on prose-prepended responses.

v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable

JUDGE_KEYS = ("faithfulness", "answer_relevance", "correctness", "context_relevance")


@runtime_checkable
class JudgeFn(Protocol):
    def __call__(
        self,
        question: str,
        expected_answer: str,
        actual_answer: str,
        retrieved_context: list[str],
    ) -> dict: ...


def score_with_judge(
    judge_fn: Optional[JudgeFn],
    question: str,
    expected_answer: str,
    actual_answer: str,
    retrieved_context: list[str],
) -> dict:
    """Calls judge_fn if supplied, validates/clamps output, never raises —
    a judge failure degrades gracefully to an empty score dict for that
    case rather than aborting the whole run."""
    if judge_fn is None or not actual_answer.strip():
        return {}
    try:
        raw = judge_fn(question, expected_answer, actual_answer, retrieved_context) or {}
    except Exception as exc:  # noqa: BLE001 — one bad judge call must not kill the run
        return {"judge_error": repr(exc)}

    scores = {}
    for key in JUDGE_KEYS:
        val = raw.get(key)
        if val is None:
            continue
        try:
            scores[key] = round(max(0.0, min(1.0, float(val))), 4)
        except (TypeError, ValueError):
            continue
    return scores


# ═══════════════════════════════════════════════════════════════
# REFERENCE IMPLEMENTATION (not wired in by default — copy into your
# own judge_fn if useful). Shows the standard RAGAS-style prompt shape
# for teams that want to use an LLM judge but haven't built one yet.
# ═══════════════════════════════════════════════════════════════

JUDGE_PROMPT_TEMPLATE = """You are evaluating a RAG chatbot's answer. Score each dimension from 0.0 to 1.0.

Question: {question}
Retrieved context: {context}
Expected answer: {expected_answer}
Actual answer: {actual_answer}

Score:
- faithfulness: does the actual answer ONLY state facts supported by the retrieved context? (1.0 = fully grounded, 0.0 = fabricated/unsupported)
- answer_relevance: does the actual answer address the question asked? (independent of whether it matches the expected answer)
- correctness: does the actual answer match the expected answer in substance? (paraphrasing is fine, factual mismatch is not)
- context_relevance: is the retrieved context actually relevant to answering the question?

Respond ONLY with JSON: {{"faithfulness": 0.0, "answer_relevance": 0.0, "correctness": 0.0, "context_relevance": 0.0}}
"""


def example_judge_fn_using_your_llm(llm_call_fn: Callable[[str], str]) -> JudgeFn:
    """
    Wraps any plain str->str LLM call function into a valid judge_fn.
    Usage:
        judge_fn = example_judge_fn_using_your_llm(your_llm_call_fn)
        run_evaluation(cases, response_fn, judge_fn=judge_fn, ...)

    your_llm_call_fn should send the prompt to whatever model you like
    (gpt-5-mini, gpt-5-nano, etc.) and return raw text. This wrapper
    handles prompt construction and JSON parsing only — no model
    coupling lives in the framework itself.

    JSON extraction mirrors the pattern already established in
    classifier_node.py's classify_intent() (v1.2.0 FIX 2): GPT-5
    reasoning models often prepend prose before the JSON object rather
    than returning it bare or backtick-fenced — a naive
    strip-and-remove-backticks parser breaks on that. Brace-position
    extraction (find "{" / rfind "}") handles all cases: plain JSON,
    fenced JSON, and prose-prepended JSON alike.
    """
    import json

    def judge_fn(question, expected_answer, actual_answer, retrieved_context):
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            question=question,
            context="\n---\n".join(retrieved_context) if retrieved_context else "(none provided)",
            expected_answer=expected_answer,
            actual_answer=actual_answer,
        )
        raw_text = (llm_call_fn(prompt) or "").strip()

        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object found in judge response: {raw_text[:100]!r}")

        clean = raw_text[start:end]
        return json.loads(clean)

    return judge_fn