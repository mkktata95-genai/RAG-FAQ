"""
Prompt Builder Node — assembles the complete user prompt from
retrieved context, conversation history, and state flags.

Runs AFTER retriever and BEFORE generator. Generator reads
state.built_prompt instead of calling build_user_prompt()
directly — making generator a pure LLM-call node with no
prompt construction logic.

Pipeline position:
    Retriever → [Prompt Builder] → Generator

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — July 2026 | Mukesh Kund
         New node — extracted from generator.py (v1.9.0) as
         part of Sprint 1 pipeline refactor.

         WHAT MOVED HERE FROM generator.py:
         - SYSTEM_PROMPT (full FCA rules prompt, v1.0.0–v1.9.0)
         - UNKNOWN_PRODUCT_RESPONSE (also kept in generator.py
           for detection after generation — both are in sync)
         - BEREAVEMENT_HANDOFF_NUMBER (same pattern)
         - build_context() (formats retrieved chunks)
         - build_user_prompt() (assembles complete user prompt)

         WHY SEPARATE FROM generator.py:
         - Single Responsibility Principle: generator.py should
           be a pure LLM-call node. Mixing prompt construction
           with LLM invocation made it 1059 lines and hard to
           maintain — changing a prompt rule required understanding
           the full generation logic.
         - Testability: build_user_prompt() can now be unit-tested
           without mocking the Azure OpenAI client.
         - Future prompt versioning (Tier 2/3): when Azure App
           Config or a prompt registry is introduced, only this
           file changes — generator.py is unaffected.

         NEW — RECOMMENDATION_RESPONSE:
         - FCA Consumer Duty compliance: Aria must not make
           personal financial recommendations. The comparison
           report (compare_indexes.py run, 25 queries) confirmed
           SAF-01 "What do you recommend?" was returning pension
           content instead of a compliant refusal. This adds both
           the constant and the RECOMMENDATION RULE in SYSTEM_PROMPT
           to fix that gap permanently.

         PROMPT VERSIONING — Tier 1 (ACTIVE):
         - Prompts loaded from code. Version tracked via Git.
         - Active version: v1.0.0 (this file's initial version).
         - Tier 2 (TODO — DevOps/Andy): Azure App Configuration
           allows prompts to be updated without redeployment.
           See commented block in prompt_builder_node() below.
         - Tier 3 (TODO — Future): Prompt registry with golden
           dataset evaluation gate — every prompt change triggers
           a regression run; deployment blocked if pass rate
           drops >2%. Depends on Tier 2 being in place.

         generator.py CHANGE (companion to this file):
         - Removed: SYSTEM_PROMPT, build_context(),
           build_user_prompt(), UNKNOWN_PRODUCT_RESPONSE import
           from here, BEREAVEMENT_HANDOFF_NUMBER import from here.
         - Added: imports SYSTEM_PROMPT, UNKNOWN_PRODUCT_RESPONSE,
           BEREAVEMENT_HANDOFF_NUMBER from this module.
         - generator_node() now reads state.built_prompt set by
           this node rather than calling build_user_prompt().

═══════════════════════════════════════════════════════════════
"""

import os
import time
import structlog
from dotenv import load_dotenv, find_dotenv

from core.schemas import AgentState

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ── Unknown Product Response ──────────────────────────────────
# Single source of truth — referenced from SYSTEM_PROMPT (f-string)
# AND imported by generator.py for post-generation detection.
# Both references must stay in sync: if the text changes here,
# generator.py's detection logic still works because it imports
# this constant (not a hardcoded string).
UNKNOWN_PRODUCT_RESPONSE = (
    "I'm sorry, I don't have information about that in our "
    "knowledge base. For assistance please contact Royal London "
    "directly on 0345 600 0371 Monday to Friday 8am to 6pm."
)

# ── Bereavement Handoff Number ────────────────────────────────
# Single source of truth for the bereavement-specific support line.
# Referenced from SYSTEM_PROMPT (HUMAN HANDOFF RULE) and
# build_user_prompt() (bereavement_note) so they cannot drift apart.
# Imported by generator.py — one number, one definition.
BEREAVEMENT_HANDOFF_NUMBER = "0370 850 2179"

# ── Recommendation Response ───────────────────────────────────
# NEW in v1.0.0 — FCA Consumer Duty compliance.
# Aria is not a financial adviser and must not make personal
# recommendations. This constant is both:
# (a) referenced in SYSTEM_PROMPT's RECOMMENDATION RULE, and
# (b) returned verbatim when the rule fires.
# Single source of truth — same pattern as UNKNOWN_PRODUCT_RESPONSE.
RECOMMENDATION_RESPONSE = (
    "As an AI assistant I'm not able to make personal financial "
    "recommendations — this is something a qualified financial "
    "adviser is best placed to help with, as they can take your "
    "individual circumstances into account.\n\n"
    "What I can do is provide general information about Royal "
    "London's products to help you understand your options. "
    "Feel free to ask me anything about pensions, life insurance, "
    "ISAs, or other Royal London products.\n\n"
    "If you'd like to speak with someone directly, you can contact "
    "Royal London on 0345 600 0371 Monday to Friday 8am to 6pm."
)

# ── System Prompt ─────────────────────────────────────────────
# ── Tier 1 (ACTIVE): Prompt loaded from code ─────────────────
# Version: v1.0.0 (initial Sprint 1 extraction from generator.py)
# All changes since generator.py v1.0.0 through v1.9.0 carried
# forward exactly. See generator.py CHANGE LOG for rationale
# behind each rule (v1.0.0–v1.9.0).
#
# New in this version: RECOMMENDATION RULE (see below).
#
# ── Tier 2 (TODO — DevOps/Andy): Azure App Configuration ─────
# When AZURE_APP_CONFIG_CONNECTION is set in .env:
#     from azure.appconfiguration import AzureAppConfigurationClient
#     config_client = AzureAppConfigurationClient.from_connection_string(
#         os.getenv("AZURE_APP_CONFIG_CONNECTION")
#     )
#     setting = config_client.get_configuration_setting(
#         key="aria-system-prompt",
#         label=os.getenv("APP_ENV", "development"),
#     )
#     SYSTEM_PROMPT = setting.value
# This allows prompt updates without redeployment.
# TODO (Andy): Provision Azure App Config resource + connection
# string in Key Vault before enabling.
#
# ── Tier 3 (TODO — Future): Prompt registry + eval gate ──────
# Every prompt change triggers golden dataset evaluation.
# Deployment blocked if pass rate drops > 2%.
# Depends on Tier 2 being in place first.

SYSTEM_PROMPT = f"""You are a helpful and professional \
customer service assistant for RLG (a UK insurance and \
pensions provider).

RESPONSE LENGTH RULE:
Keep responses concise and mobile-friendly.
Maximum 300 words. Use bullet points for lists.
No unnecessary repetition or padding.

RECOMMENDATION RULE:
If the customer asks Aria to recommend, suggest, or personally
choose between financial products (for example "what do you
recommend?", "which pension should I choose?", "what's best
for me?", "what would you suggest?", "which is better for me?")
— do NOT make a recommendation.
Respond with exactly:
"{RECOMMENDATION_RESPONSE}"
This is a regulatory boundary — Aria is not a financial adviser.
This rule takes priority over the UNKNOWN PRODUCT RULE and all
ANSWER RULES below.

EMPATHY RULE:
ONLY apply empathy if the customer explicitly mentions
one of these genuine distress situations:
  - Terminal or life-threatening illness (cancer, terminal)
  - Death or bereavement (died, passed away, bereavement)
  - Critical illness or serious disability
  - Redundancy or losing their job
  - Serious financial hardship (cannot afford, struggling to pay)
  - Divorce or separation
  - Mental health issues (anxiety, depression)
  - Being diagnosed with a serious medical condition

When one of the above is present — acknowledge with genuine
empathy in 1-2 sentences BEFORE answering.
Example: "I'm truly sorry to hear about your situation.
I hope the following information is helpful..."

DO NOT apply empathy for these standard administrative tasks —
they are normal processes, not sensitive situations:
  - Finding or tracing a lost pension
  - Making a claim (standard insurance process)
  - Transferring a pension
  - Updating personal details
  - Checking policy information
  - Any general product or process question

HUMAN HANDOFF RULE:
ONLY add the handoff message when the customer has mentioned
one of the genuine distress situations listed in EMPATHY RULE
above (terminal illness, bereavement, redundancy etc).
When applicable — always end with:
"For personalised support, one of our advisers would be
happy to help. Please call us on 0345 600 0371
Monday to Friday 8am to 6pm."
Do NOT add handoff message for standard administrative
queries (lost pension, making a claim, transfers etc).
If the user prompt below contains a NOTE specifying an
alternative phone number for this response (for example,
the dedicated bereavement support line), use THAT number
INSTEAD of 0345 600 0371 in the handoff message above —
for the handoff message only. Do not change any other
phone number in your response.
Decide the handoff number for THIS response on its own —
do NOT copy a phone number from a previous assistant turn
shown in the conversation history above. If this
response's NOTE does not specify an alternative number,
use 0345 600 0371, even if a different number appeared in
an earlier turn of this conversation.

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
Only attach a [1][2][3] marker to a sentence that
contains a claim drawn directly from the provided
context. Do NOT attach citation markers to a sentence
that says information is unavailable, cannot be
confirmed, or that directs the customer to contact
Royal London or speak to an adviser — those sentences
are not claims from the sources and must carry no [n]
markers.
Do NOT add a "Sources:" or "Source:" section, list
source URLs as plain text, or otherwise repeat source
links anywhere in your response. The [1][2][3] markers
are sufficient on their own — the interface displays the
source links separately. The URL shown in brackets after
each source label in the context below is for your
reference only, to identify which [n] corresponds to
which source — never reproduce that URL in your answer.
IMPORTANT — DOMAIN RESTRICTION:
Only cite sources from royallondon.com.
Do NOT include or link to any external website URLs
in your response — even if they appear in the context.
IMPORTANT — ORGANISATION BLOCKLIST:
NEVER mention any of these organisations by name,
even if they appear in the provided context.
Completely ignore any reference to them in the context:
  MoneyHelper, Money Helper, MaPS,
  Pension Tracing Service, Pension Wise, Pension Advisory,
  Policy Detective, Citizens Advice, Citizens Bureau,
  StepChange, National Debt Line, Payplan,
  Age UK, Age England, Age Scotland,
  Which?, MoneySavingExpert, Martin Lewis,
  Financial Ombudsman, FCA, FCA Register,
  Turn2Us, Experian, Equifax, TransUnion,
  Cruse, Samaritans, BACP, Marie Curie,
  HMRC, DWP, Jobcentre, Universal Credit.
If the context references any of these — skip that part
of the context entirely. Do not paraphrase it either.
This is an RLG-only assistant. For anything outside
Royal London's products direct to 0345 600 0371.

UNKNOWN PRODUCT RULE:
Royal London offers: life insurance, pensions, ISAs,
critical illness cover, income protection, and over 50s
life insurance. If the customer asks about a product or
service that does NOT appear in the provided context AND
is not one of the above Royal London products — do NOT
attempt to answer or use general knowledge.
Instead respond with exactly:
"{UNKNOWN_PRODUCT_RESPONSE}"
Examples of products Royal London does NOT offer:
credit cards, bank accounts, mortgages, car insurance,
home insurance, travel insurance, cryptocurrency.

PRODUCT CATEGORY QUESTIONS RULE:
This rule is an exception to ANSWER RULE 2 below, scoped
narrowly as follows.
If the customer asks what TYPES, KINDS, or OPTIONS exist
within one of Royal London's product categories listed in
the UNKNOWN PRODUCT RULE above (for example "what types of
pensions does Royal London offer", "what kinds of life
insurance do you have", "what ISA options are there") —
and the provided context contains general explanatory
content about that category (for example a page explaining
what a pension is and the different types of pension) —
answer using that content, even if it is not phrased as
"Royal London offers...".
This exception does NOT apply to questions asking for a
SPECIFIC fact about Royal London's own products or a named
customer's policy — for example exact growth rates,
guaranteed amounts, specific payout or premium figures, or
personal eligibility. Those remain governed by ANSWER RULE 2:
only state such figures if explicitly given in the context.
Only fall back to the UNKNOWN PRODUCT RULE response above if
the context contains no relevant explanatory content about
that product category at all.

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
- Apply empathy to standard administrative queries
- Add human handoff to standard administrative queries
"""


# ── Prompt construction helpers ───────────────────────────────
def build_context(state: AgentState) -> str:
    """
    Format retrieved chunks into numbered context block.
    Includes source title and URL so the model can map
    [1][2][3] citations to the correct sources.

    Moved from generator.py v1.9.0. Logic unchanged.
    """
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
    """
    Assemble the complete user prompt from:
    - Conversation history (last 6 turns)
    - Context-specific notes (empathy, bereavement, disclaimer,
      override)
    - Retrieved context from build_context()
    - The customer's query

    Moved from generator.py v1.9.0. Logic unchanged.
    All bug fixes (v1.5.0–v1.9.0) carried forward exactly:
    - v1.5.0: empathy_note
    - v1.7.0: bereavement_note using BEREAVEMENT_HANDOFF_NUMBER
    - v1.5.0: disclaimer_note
    - v1.9.0: override_note anchored to last User turn
    - v1.9.0: override context block omission when chunks empty
    """
    context = build_context(state)

    # Conversation history — last 6 turns
    history = ""
    if state.conversation_history:
        recent = state.conversation_history[-6:]
        parts  = []
        for turn in recent:
            role    = turn.get("role", "")
            content = turn.get("content", "")
            parts.append(f"{role.capitalize()}: {content}")
        history = (
            "Previous conversation:\n"
            + "\n".join(parts)
            + "\n\n"
        )

    # Empathy note — injected when supervisor detected sensitive
    # disclosure (terminal illness, bereavement, redundancy etc).
    # state.needs_empathy set by supervisor.py BEFORE cache_check.
    empathy_note = ""
    if state.needs_empathy:
        empathy_note = (
            "NOTE: This customer is dealing with a sensitive "
            "situation. Please acknowledge with empathy first.\n\n"
        )

    # Bereavement note (v1.7.0) — strict subset of empathy.
    # Tells GPT to use the dedicated bereavement support line
    # for the human handoff in THIS response only.
    bereavement_note = ""
    if state.__dict__.get("_bereavement"):
        bereavement_note = (
            "NOTE: This query relates to a bereavement. For the "
            "HUMAN HANDOFF RULE in this response, use the "
            f"dedicated bereavement support line "
            f"{BEREAVEMENT_HANDOFF_NUMBER} instead of "
            "0345 600 0371. Do not change any other phone "
            "number in your response — only the human handoff "
            "number.\n\n"
        )

    # Disclaimer note — injected when supervisor detected a
    # financial decision query.
    disclaimer_note = ""
    if state.needs_disclaimer:
        disclaimer_note = (
            "NOTE: This query involves a financial decision. "
            "Please add the financial disclaimer as plain text "
            "at the end. Do NOT wrap it in asterisks or any "
            "markdown formatting whatsoever.\n\n"
        )

    # Override note (v1.9.0) — injected when classifier detected
    # a contextual follow-up. Explicitly anchors to the LAST User
    # turn in history to prevent the model anchoring to the most
    # emotionally prominent earlier turn instead.
    override_note = ""
    if state.__dict__.get("_override_triggered"):
        last_user_q = ""
        if state.conversation_history:
            recent = state.conversation_history[-6:]
            for turn in reversed(recent):
                if turn.get("role", "").lower() == "user":
                    last_user_q = turn.get("content", "").strip()
                    break

        if last_user_q:
            override_note = (
                "NOTE: The customer is referring to a previous "
                "exchange in the conversation above. "
                f"The customer's PREVIOUS question (the one "
                f"immediately before this message) was: "
                f"'{last_user_q}'. "
                "Address THAT question specifically — do NOT "
                "anchor to an earlier turn even if it was more "
                "emotionally prominent. "
                "You DO have access to the conversation history "
                "shown. Acknowledge what was previously discussed "
                "and respond based on that context. "
                "Do NOT say you have no information about this — "
                "use the conversation history to understand what "
                "they are asking about and respond helpfully. "
                "If they are expressing frustration, acknowledge "
                "it genuinely, clarify your previous answer, and "
                "offer further help or the phone number "
                "0345 600 0371.\n\n"
            )

    # v1.9.0: when override is active, retriever.py v1.1.0 returns
    # empty chunks. Omit the context block entirely — the model
    # should use conversation history only, not be given an empty
    # "Context from RLG documentation: (nothing)" block.
    is_override_active = (
        state.__dict__.get("_override_triggered")
        and not state.retrieved_chunks
    )

    if is_override_active:
        return (
            f"{history}"
            f"{empathy_note}"
            f"{bereavement_note}"
            f"{disclaimer_note}"
            f"{override_note}"
            f"Customer question: {state.query}\n\n"
            f"Use the conversation history above to answer. "
            f"Do NOT cite any sources or add [1][2][3] markers "
            f"— there are no retrieved documents for this query."
        )

    return (
        f"{history}"
        f"{empathy_note}"
        f"{bereavement_note}"
        f"{disclaimer_note}"
        f"{override_note}"
        f"Context from RLG documentation:\n\n"
        f"{context}\n\n"
        f"Customer question: {state.query}\n\n"
        f"Answer using only the context above. "
        f"If the question is about a product or service not "
        f"covered in the context, follow the UNKNOWN PRODUCT RULE. "
        f"Cite sources sequentially as [1][2][3] "
        f"in order of first use."
    )


# ── Main node ─────────────────────────────────────────────────
def prompt_builder_node(state: AgentState) -> AgentState:
    """
    Build the complete user prompt and store on state.built_prompt.

    Runs AFTER retriever (needs state.retrieved_chunks) and
    BEFORE generator (which reads state.built_prompt).

    Prompt versioning:
    - Tier 1 (ACTIVE): prompts from code, versioned via Git.
    - Tier 2 (TODO): Azure App Config — see SYSTEM_PROMPT comment.
    - Tier 3 (TODO): Prompt registry + eval gate.

    Skips prompt building if state.refusal_triggered is True
    (retriever found no results, input safety blocked etc) —
    generator won't run in that case so building the prompt
    would be wasted work.

    On any error: sets state.built_prompt to a minimal safe
    fallback so generator_node() can still attempt a response
    rather than failing silently.
    """
    start = time.time()

    # Skip if pipeline already terminated upstream
    if state.refusal_triggered:
        log.info(
            "prompt_builder_skipped",
            reason="refusal_triggered",
            request_id=state.request_id,
        )
        return state

    try:
        # ── Tier 1: Build prompt from code ────────────────────
        # TODO (Tier 2 — Andy): load from Azure App Config when
        # AZURE_APP_CONFIG_CONNECTION env var is set.
        # See SYSTEM_PROMPT comment block above for code snippet.
        built_prompt = build_user_prompt(state)
        state.built_prompt = built_prompt

        latency = (time.time() - start) * 1000
        state.latency_ms["prompt_builder"] = latency

        log.info(
            "prompt_built",
            prompt_length=len(built_prompt),
            has_context=bool(state.retrieved_chunks),
            has_history=bool(state.conversation_history),
            needs_empathy=state.needs_empathy,
            needs_disclaimer=state.needs_disclaimer,
            bereavement=bool(state.__dict__.get("_bereavement")),
            override=bool(state.__dict__.get("_override_triggered")),
            latency_ms=round(latency),
            request_id=state.request_id,
        )

    except Exception as e:
        # Fallback: minimal prompt — generator can still run
        log.error(
            "prompt_builder_error",
            error=str(e),
            request_id=state.request_id,
        )
        state.built_prompt = (
            f"Customer question: {state.query}\n\n"
            f"Answer using only information about Royal London's "
            f"insurance, pensions and ISA products."
        )

    return state