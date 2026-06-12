"""
Generator Node — LLM response with empathy, disclaimer,
answer length control and token tracking.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         Mistral Large, API key auth, basic generation

v1.1.0 — Migration: Mistral → gpt-4.1 + gpt-4o-mini
         - Main model: Mistral Large → gpt-4.1
           (complex queries, sensitive topics, empathy)
         - Fast model: Mistral Small → gpt-4o-mini
           (simple FAQs, intent classification)
         - Auth: API key → DefaultAzureCredential + bearer token
         - Added UNKNOWN PRODUCT RULE to system prompt:
           prevents hallucination when retrieved context does
           not match the query (e.g. credit card queries)

v1.2.0 — June 2026 | Mukesh Kund
         External URL citation fix + account access rule

         SYSTEM PROMPT — CITATION RULE [MODIFIED]:
         - Added domain restriction: only cite royallondon.com
         - GPT must not include or link to external URLs even
           if they appear in the retrieved chunk content
         - External service names may be mentioned (e.g.
           "the government's Pension Tracing Service") but
           their URLs must never appear in the response
         - Fixes: MoneyHelper and Pension Tracing Service
           appearing as clickable links inside answers

         SYSTEM PROMPT — ACCOUNT ACCESS RULE [NEW RULE]:
         - Aria does not have access to any customer accounts,
           policy records, or personal data
         - If customer provides NI number, policy number, DOB
           or any personal details and asks for a lookup —
           Aria must refuse and direct to 0345 600 0371
         - Fixes: query "My NI number is AB123456C, can you
           look up my pension?" was giving pension-finding
           steps instead of refusing the account lookup

v1.3.0 — June 2026 | Mukesh Kund
         Empathy trigger refinement + account access hardening
         + external service name restriction

         EMPATHY_TRIGGERS [MODIFIED]:
         - Removed triggers that are administrative tasks,
           not genuine sensitive/distressing situations:
             REMOVED: 'claim', 'make a claim' — standard FAQ
             REMOVED: 'lost pension' — administrative task
             REMOVED: 'condition' — too broad, matches too many
             REMOVED: 'illness' — too broad (e.g. "critical illness
               cover" is a product query, not a sensitive situation)
             REMOVED: 'injury' — too broad
             REMOVED: 'accident' — too broad
             REMOVED: 'hospital' — too broad
           - These were causing empathy to fire on standard FAQ
             queries like "How do I make a claim?" and
             "How do I find a lost pension?" — incorrect behaviour
             that made responses feel inappropriate and generic
           - Kept all triggers that represent genuine distress:
             terminal illness, bereavement, death, redundancy,
             financial hardship, divorce, mental health

         SYSTEM PROMPT — ACCOUNT ACCESS RULE [STRENGTHENED]:
         - Added explicit stop instruction: after the refusal
           message, do not add any further guidance or steps
         - Fixes: after refusing NI number account lookup, GPT
           was continuing with pension-finding guidance because
           it had pension-related chunks in context
         - Now: refusal message is the complete response, full stop

         SYSTEM PROMPT — CITATION RULE [STRENGTHENED]:
         - Added explicit restriction on mentioning external
           service names as recommendations
         - GPT may acknowledge a service exists (e.g. "the
           government's Pension Tracing Service") but must NOT
           recommend it or present it as a resource to use
         - Fixes: "you can also use services like MoneyHelper..."
           appearing in responses — this is an RLG-only chatbot

═══════════════════════════════════════════════════════════════
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
# IMPORTANT: Only include triggers that represent GENUINE distress
# or sensitive personal circumstances — NOT administrative tasks.
#
# REMOVED in v1.3.0 (were causing false positives):
#   'claim', 'make a claim' → standard FAQ, not sensitive
#   'lost pension'          → administrative task, not distress
#   'illness'               → too broad, matches 'critical illness cover'
#   'condition'             → too broad, matches product descriptions
#   'injury'                → too broad
#   'accident'              → too broad
#   'hospital'              → too broad
#
EMPATHY_TRIGGERS = [
    # Terminal / life-threatening illness
    "cancer", "terminal", "critically ill", "serious illness",
    "life-limiting", "life limiting",
    # Bereavement
    "died", "death", "bereavement", "passed away",
    "losing someone", "loss of a loved",
    # Disability
    "disability", "disabled",
    # Employment / financial hardship
    "redundan", "unemployed", "losing my job", "lost my job",
    "financial difficulty", "financial hardship",
    "can't afford", "cannot afford", "struggling to pay",
    # Relationship breakdown
    "divorce", "separation", "separating",
    # Mental health
    "mental health", "anxiety", "depression",
    # Diagnosis
    "diagnosed",
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
IMPORTANT — DOMAIN RESTRICTION:
Only cite sources from royallondon.com.
Do NOT include or link to any external website URLs
in your response — even if they appear in the context.
Do NOT recommend or suggest external services, websites,
or organisations as resources for the customer to use.
You may acknowledge that government services exist
(e.g. "the government offers a pension tracing service")
but do NOT name specific third-party organisations,
do NOT present them as recommendations, and do NOT
suggest the customer use them.
This is an RLG-only assistant. Direct all further
help to 0345 600 0371.

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

ACCOUNT ACCESS RULE:
You do NOT have access to any customer accounts, policy
details, pension records, or personal data of any kind.
If a customer asks you to look up, check, retrieve, or
access their account, policy, pension, or personal
information — do NOT attempt to do so.
Respond with ONLY this exact message and nothing else:
"I'm not able to access account information directly.
For your account details please call us on
0345 600 0371 Monday to Friday 8am to 6pm."
Do NOT add any further guidance, steps, resources,
or helpful information after this message.
Stop there. The refusal is your complete response.
This applies even if the customer provides their
NI number, policy number, date of birth, or any
other personal details.

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
- Recommend or name third-party organisations or services
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