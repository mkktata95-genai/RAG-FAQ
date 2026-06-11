"""
Generator Node — LLM response with empathy, disclaimer,
answer length control and token tracking.

Migration: Mistral → gpt-4.1 (main) + gpt-4o-mini (simple)
Auth:       DefaultAzureCredential + bearer token (no API key)
Fix:        Added UNKNOWN PRODUCT RULE to system prompt —
            prevents hallucination when context doesn't match query
"""

import os
import re
import time
import structlog
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv

from core.schemas import AgentState, Citation
from core.middleware import track_token_usage

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT  = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
DEPLOYMENT_MAIN        = os.getenv("AZURE_OPENAI_DEPLOYMENT_MAIN", "gpt-4.1")
DEPLOYMENT_FAST        = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o-mini")

# ── Singleton client ──────────────────────────────────────────
_credential:    DefaultAzureCredential | None = None
_openai_client: AzureOpenAI | None            = None


def get_credential() -> DefaultAzureCredential:
    """Get or create singleton DefaultAzureCredential."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_openai_client() -> AzureOpenAI:
    """
    Get or create singleton AzureOpenAI client.
    Reused across all generator calls.
    """
    global _openai_client
    if _openai_client is None:
        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT is not set in .env"
            )
        token_provider = get_bearer_token_provider(
            get_credential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _openai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
        log.info(
            "generator_client_created",
            endpoint=AZURE_OPENAI_ENDPOINT,
        )
    return _openai_client


# ── Empathy Detection ─────────────────────────────────────────
EMPATHY_TRIGGERS = [
    "cancer", "terminal", "died", "death", "bereavement",
    "critically ill", "serious illness", "disability",
    "redundan", "unemployed", "struggling",
    "financial difficulty", "financial hardship",
    "divorce", "separation", "accident", "injury",
    "diagnosed", "illness", "condition", "hospital",
    "passed away", "losing my job", "can't afford",
    "mental health", "anxiety", "depression",
]

# ── Financial Decision Detection ──────────────────────────────
FINANCIAL_DECISION_TRIGGERS = [
    "should i", "should i invest", "which pension",
    "best option", "recommend", "advice",
    "what should", "is it worth", "better to",
    "choose", "decide", "switch", "transfer",
    "how much should", "when should",
    "tax", "return", "growth", "performance",
]

# ── System Prompt ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are a helpful and professional \
customer service assistant for RLG (a UK insurance and \
pensions provider).

RESPONSE LENGTH RULE:
Keep responses concise and mobile-friendly.
Maximum 300 words. Use bullet points for lists.
No unnecessary repetition or padding.

EMPATHY RULE:
If the customer mentions illness, bereavement, disability,
financial difficulty, divorce or any sensitive personal
circumstance — acknowledge their situation with genuine
empathy in 1-2 sentences BEFORE answering.
Example: "I'm truly sorry to hear about your situation.
I hope the following information is helpful..."

HUMAN HANDOFF RULE:
For sensitive queries (illness, bereavement, financial
hardship) — always end with:
"For personalised support, one of our advisers would be
happy to help. Please call us on 0345 600 0371
Monday to Friday 8am to 6pm."

FINANCIAL DISCLAIMER RULE:
ONLY add this disclaimer when the query involves a
financial decision, investment choice, or personal
financial advice. Write it as plain text with NO
asterisks or markdown formatting:
Please note: This information is for general guidance
only and does not constitute financial advice. For advice
tailored to your personal circumstances, we recommend
speaking with a qualified financial adviser or contacting
us directly on 0345 600 0371.

CITATION RULE:
Always cite sources sequentially starting from [1].
Use [1] for the first source you reference,
[2] for the second, [3] for the third.
Never skip numbers or use numbers out of order.

UNKNOWN PRODUCT RULE:
Royal London offers: life insurance, pensions, ISAs,
critical illness cover, income protection, and over 50s
life insurance. If the customer asks about a product or
service that does NOT appear in the provided context AND
is not one of the above Royal London products — do NOT
attempt to answer or use general knowledge.
Instead respond with exactly:
"I'm sorry, I don't have information about that in our
knowledge base. For assistance please contact Royal London
directly on 0345 600 0371 Monday to Friday 8am to 6pm."
Examples of products Royal London does NOT offer:
credit cards, bank accounts, mortgages, car insurance,
home insurance, travel insurance, cryptocurrency.

ANSWER RULES:
1. Answer ONLY from the provided context
2. ONLY include facts explicitly stated in context
   Do NOT add general knowledge or assumptions
3. Cite sources inline as [1][2][3] sequentially
4. Never make up information not in context
5. Use formal professional British English
6. If context insufficient say so formally
   and direct to 0345 600 0371

NEVER:
- Recommend specific products for personal situations
- Make guarantees about returns or payouts
- Provide legal advice
- Discuss competitor products negatively
- Use asterisks around disclaimer text
- Answer questions about products not in the context
"""


# ── Helper functions ──────────────────────────────────────────
def needs_empathy(query: str) -> bool:
    query_lower = query.lower()
    return any(t in query_lower for t in EMPATHY_TRIGGERS)


def needs_disclaimer(query: str) -> bool:
    query_lower = query.lower()
    return any(
        t in query_lower for t in FINANCIAL_DECISION_TRIGGERS
    )


def is_simple_query(query: str) -> bool:
    query_lower = query.lower()
    complex_indicators = [
        "compare" in query_lower,
        "difference between" in query_lower,
        "explain" in query_lower,
        "calculate" in query_lower,
        len(query.split()) > 20,
        query.count("?") > 1,
        needs_empathy(query),
    ]
    simple_indicators = [
        len(query.split()) < 10,
        "?" in query and query.count("?") == 1,
        any(w in query_lower for w in [
            "what is", "how do i", "can i",
            "where", "when", "who", "contact",
            "phone", "number",
        ]),
    ]
    if any(complex_indicators):
        return False
    if sum(simple_indicators) >= 2:
        return True
    return False


def build_context(state: AgentState) -> str:
    """Build context string with title for better LLM answers."""
    parts = []
    for i, chunk in enumerate(state.retrieved_chunks, 1):
        source_label = (
            chunk.title if chunk.title
            else chunk.section
        )
        parts.append(
            f"[{i}] Source: {source_label} "
            f"({chunk.source_url})\n{chunk.content}"
        )
    return "\n\n---\n\n".join(parts)


def build_user_prompt(state: AgentState) -> str:
    context = build_context(state)

    history = ""
    if state.conversation_history:
        recent = state.conversation_history[-6:]
        parts = []
        for turn in recent:
            role    = turn.get("role", "")
            content = turn.get("content", "")
            parts.append(f"{role.capitalize()}: {content}")
        history = (
            "Previous conversation:\n"
            + "\n".join(parts)
            + "\n\n"
        )

    empathy_note = ""
    if state.needs_empathy:
        empathy_note = (
            "NOTE: This customer is dealing with a sensitive "
            "situation. Please acknowledge with empathy first.\n\n"
        )

    disclaimer_note = ""
    if state.needs_disclaimer:
        disclaimer_note = (
            "NOTE: This query involves a financial decision. "
            "Please add the financial disclaimer as plain text "
            "at the end. Do NOT wrap it in asterisks or any "
            "markdown formatting whatsoever.\n\n"
        )

    return (
        f"{history}"
        f"{empathy_note}"
        f"{disclaimer_note}"
        f"Context from RLG documentation:\n\n"
        f"{context}\n\n"
        f"Customer question: {state.query}\n\n"
        f"Answer using only the context above. "
        f"If the question is about a product or service not "
        f"covered in the context, follow the UNKNOWN PRODUCT RULE. "
        f"Cite sources sequentially as [1][2][3] "
        f"in order of first use."
    )


def clean_response_text(text: str) -> str:
    """
    Post-process LLM response to fix formatting issues.
    Removes asterisk wrapping from disclaimer lines.
    """
    lines   = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if (
            stripped.startswith("*")
            and stripped.endswith("*")
            and not stripped.startswith("**")
            and not stripped.startswith("* ")
        ):
            line = line.replace(stripped, stripped[1:-1])
        cleaned.append(line)
    return "\n".join(cleaned)


def extract_citations(
    state: AgentState,
    response_text: str,
) -> tuple[str, list[Citation]]:
    """
    Extract citations and renumber sequentially
    by order of first appearance in text.
    Returns updated response text + citations list.
    """
    all_markers = re.findall(r'\[(\d+)\]', response_text)

    if not all_markers:
        return response_text, []

    # Build mapping: original number → new sequential number
    seen_order = {}
    counter    = 1
    for num in all_markers:
        if num not in seen_order:
            seen_order[num] = counter
            counter += 1

    # Renumber in response text
    def replace_citation(match):
        orig    = match.group(1)
        new_num = seen_order.get(orig, orig)
        return f"[{new_num}]"

    updated_text = re.sub(
        r'\[(\d+)\]', replace_citation, response_text
    )

    # Build citations list using new numbering
    citations = []
    seen_urls = set()

    for orig_num, new_num in sorted(
        seen_order.items(), key=lambda x: x[1]
    ):
        idx = int(orig_num) - 1
        if 0 <= idx < len(state.retrieved_chunks):
            chunk = state.retrieved_chunks[idx]
            if chunk.source_url not in seen_urls:
                citations.append(Citation(
                    index=new_num,
                    url=chunk.source_url,
                    section=chunk.section,
                    title=chunk.title,
                ))
                seen_urls.add(chunk.source_url)

    return updated_text, citations


# ── Main node ─────────────────────────────────────────────────
def generator_node(state: AgentState) -> AgentState:
    """Generate response using gpt-4.1 or gpt-4o-mini."""
    start = time.time()

    state.needs_empathy    = needs_empathy(state.query)
    state.needs_disclaimer = needs_disclaimer(state.query)
    state.is_sensitive     = state.needs_empathy

    try:
        # Route: simple queries → gpt-4o-mini, complex → gpt-4.1
        deployment = (
            DEPLOYMENT_FAST
            if is_simple_query(state.query)
            else DEPLOYMENT_MAIN
        )

        client = get_openai_client()

        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {
                    "role":    "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role":    "user",
                    "content": build_user_prompt(state),
                },
            ],
            max_tokens=800,
            temperature=0.1,
        )

        raw_response     = response.choices[0].message.content
        state.model_used = deployment

        # Clean formatting issues
        raw_response = clean_response_text(raw_response)

        # Extract + renumber citations
        updated_text, citations = extract_citations(
            state, raw_response
        )
        state.raw_response = updated_text
        state.citations    = citations

        # Track token usage
        usage = response.usage
        if usage:
            state.token_usage = {
                "input_tokens":  usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "total_tokens":  usage.total_tokens,
            }
            track_token_usage(
                model=deployment,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            )

        latency = (time.time() - start) * 1000
        state.latency_ms["generator"] = latency

        log.info(
            "generation_complete",
            model=deployment,
            citations=len(citations),
            latency_ms=round(latency),
            empathy=state.needs_empathy,
            disclaimer=state.needs_disclaimer,
            request_id=state.request_id,
            tokens=state.token_usage.get("total_tokens", 0),
        )

    except Exception as e:
        log.error(
            "generator_error",
            error=str(e),
            request_id=state.request_id,
        )
        from core.refusal import get_refusal, RefusalReason
        state.refusal_triggered = True
        state.final_response    = get_refusal(
            RefusalReason.GENERAL
        )

    return state