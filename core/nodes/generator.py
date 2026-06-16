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

v1.4.0 — June 2026 | Mukesh Kund
         Empathy rule precision + organisation blocklist

         SYSTEM PROMPT — EMPATHY RULE [REWRITTEN]:
         - Root cause: GPT was independently applying empathy
           to standard administrative queries ("How do I find
           a lost pension?", "How do I make a claim?") because
           the EMPATHY RULE said "sensitive personal circumstance"
           which GPT interpreted too broadly
         - Fix: replaced vague rule with explicit QUALIFYING LIST
           (terminal illness, bereavement, death, redundancy,
           financial hardship, divorce, mental health, diagnosis)
           and explicit NON-QUALIFYING LIST (finding lost pension,
           making a claim, transfers, updating details, product
           questions) — no room for GPT interpretation
         - Python needs_empathy() detection already correct
           (empathy=False in logs) — this fix aligns the system
           prompt with what Python was already detecting

         SYSTEM PROMPT — HUMAN HANDOFF RULE [UPDATED]:
         - Aligned with new EMPATHY RULE — only fires on same
           explicit list of genuine distress situations
         - Prevents handoff message appearing on standard queries

         SYSTEM PROMPT — CITATION RULE [BLOCKLIST ADDED]:
         - Added explicit organisation blocklist of 30+ names
           that must NEVER appear in responses even if present
           in retrieved context:
           MoneyHelper, Pension Tracing Service, Policy Detective,
           Citizens Advice, StepChange, Age UK, Which?,
           MoneySavingExpert, Financial Ombudsman, HMRC etc.
         - Instruction: skip that part of context entirely,
           do not paraphrase it either
         - Fixes: "MoneyHelper can assist..." appearing in
           lost pension responses

         SYSTEM PROMPT — NEVER LIST [UPDATED]:
         - Added: "Apply empathy to standard administrative queries"
         - Added: "Add human handoff to standard administrative queries"

v1.5.0 — June 2026 | Mukesh Kund
         History summary note for context override queries

         build_user_prompt() [MODIFIED]:
         - When state._override_triggered=True (set by supervisor
           when a follow-up/frustration query is detected),
           now injects an explicit note into the user prompt:
             "NOTE: The customer is referring to the previous
              conversation. You have conversation history above.
              Acknowledge what was discussed and respond based
              on that context. Do not say you have no information
              — use the history."
         - WHY: without this note, GPT received the follow-up
           query ("Why didn't you answer?") with conversation
           history in the prompt but still fired the UNKNOWN
           PRODUCT RULE because the retrieved chunks were
           irrelevant (canonical rewrite had destroyed the query).
           Now: canonical rewrite skipped (cache_check v1.2.0)
           AND generator explicitly told to use history.
           Two complementary fixes working together.

v1.6.0 — June 2026 | Mukesh Kund
         Empathy/disclaimer detection moved to supervisor.py +
         UNKNOWN PRODUCT RULE refusal no longer cacheable

         PART 1 — EMPATHY_TRIGGERS / FINANCIAL_DECISION_TRIGGERS /
         needs_empathy() / needs_disclaimer() [REMOVED — MOVED to
         supervisor.py v1.5.0]:
         - ROOT CAUSE: cache_check runs BEFORE generator. A cache
           hit (direct match or Stage 3 canonical rewrite) routes
           straight to END — generator_node() never ran, so these
           flags were never computed for cached responses.
           Reproduced live: "I have been diagnosed with terminal
           cancer" and "My wife passed away last week" were both
           canonical-rewritten to generic FAQ phrasing in
           cache_check, hit the cache at similarity=1.0, and
           skipped empathy/disclaimer/handoff entirely.
         - FIX: supervisor.py now computes state.needs_empathy
           and state.needs_disclaimer immediately after
           expand_query() — before cache_check runs — so
           cache_check/cache_write can skip the semantic cache
           for sensitive disclosures (see their v1.5.0/v1.1.0
           changelogs).
         - generator_node() [MODIFIED]:
           - Removed the recompute lines:
               state.needs_empathy    = needs_empathy(state.query)
               state.needs_disclaimer = needs_disclaimer(state.query)
               state.is_sensitive     = state.needs_empathy
           - These flags now arrive on state already set by
             supervisor — generator only READS them.
         - is_simple_query() [MODIFIED]:
           - Signature changed from is_simple_query(query) to
             is_simple_query(query, is_sensitive) — the
             "needs_empathy" complex-indicator is now passed in
             from state.is_sensitive instead of being recomputed
             locally (needs_empathy() no longer exists here).

         PART 2 — UNKNOWN PRODUCT RULE refusal made non-cacheable:
         - ROOT CAUSE: the UNKNOWN PRODUCT RULE response ("I'm
           sorry, I don't have information about that in our
           knowledge base...") is produced as a normal
           generation_complete result (citations=0,
           refusal_triggered stays False) — NOT via
           refusal_triggered/refusal.py. cache_write_node only
           skips on refusal_triggered, so this wrong "no
           information" answer was being cached and then served
           for OTHER phrasings of the same topic too (e.g. a
           wrongly-refused "What types of pensions does Royal
           London offer?" answer was returned, cached, and then
           replayed verbatim for "What pension products does
           Royal London offer", Cached 357ms).
         - UNKNOWN_PRODUCT_RESPONSE [NEW CONSTANT]:
           Single source of truth for the exact refusal text.
           Referenced from SYSTEM_PROMPT (f-string) so the prompt
           and the detection check can never drift apart.
         - generator_node() [MODIFIED]:
           After extract_citations(), if the response text
           contains UNKNOWN_PRODUCT_RESPONSE, set
           state.refusal_triggered = True and
           state.final_response = UNKNOWN_PRODUCT_RESPONSE
           (state.citations cleared to []). This reuses the
           existing refusal_triggered routing
           (route_after_generator → END), which already makes
           cache_write_node skip via its
           "refusal_triggered or not final_response" check — no
           change needed to cache_write.py for this part.
         - NOTE: this fixes the SYMPTOM (wrong refusals spreading
           via cache). The underlying generator over-refusal for
           legitimate "what does Royal London offer" style
           queries (SYSTEM_PROMPT / model-routing tuning) is a
           separate, lower-risk follow-up item — flagged but not
           addressed in this change.

v1.7.0 — June 2026 | Mukesh Kund
         PART 1 — Punctuation-dependent over-refusal
         (PRODUCT CATEGORY QUESTIONS RULE)

         ROOT CAUSE — confirmed via paired live repro
         (request_id=8daf9246-... vs 99e63e54-...), context
         (chunks_found=5, retrieval scores) equivalent in both:
           "What types of pensions does Royal London offer?"
             -> is_simple_query: word_count<10 (+1), "?" with
                count==1 (+1), no keyword match (+0) = 2/2
             -> routed to gpt-4o-mini
             -> citations=0, refusal_triggered=True
                (UNKNOWN_PRODUCT_RESPONSE)
           "What pension products does Royal London offer"
             (no "?")
             -> is_simple_query: word_count<10 (+1), no "?" (+0),
                no keyword match (+0) = 1/2
             -> routed to gpt-4.1
             -> citations=2, correct cited answer
         Both phrasings retrieved the same "What is a pension &
         the different types" page (general explanatory content,
         not phrased as "Royal London offers..."). gpt-4o-mini
         over-applied ANSWER RULE 2 ("ONLY include facts
         explicitly stated in context, do NOT add general
         knowledge") against this chunk and fell through to
         UNKNOWN PRODUCT RULE; gpt-4.1 did not. The customer's
         outcome depended entirely on a trailing "?" choosing
         gpt-4o-mini vs gpt-4.1 — for an FCA-regulated assistant,
         two equivalent phrasings of "what pension types do you
         offer" producing "here are the types" vs "I have no
         information, call us" is a Consumer Duty issue, not just
         an inconsistency.

         FIX — SYSTEM PROMPT, new PRODUCT CATEGORY QUESTIONS RULE
         [NEW RULE, placed immediately after UNKNOWN PRODUCT RULE]:
         - Scoped narrowly: applies ONLY when (a) the customer
           asks what TYPES/KINDS/OPTIONS exist within a product
           CATEGORY already on Royal London's offered-products
           list (pensions, ISAs, life insurance, critical illness
           cover, income protection, over 50s life insurance), AND
           (b) the context contains general explanatory content
           about that category.
         - In that case, the model should answer from that
           content even if it is not phrased as "Royal London
           offers...".
         - Explicitly does NOT relax ANSWER RULE 2 for queries
           asking SPECIFIC facts (growth rates, guaranteed
           amounts, payout/premium figures, eligibility) — those
           remain governed by ANSWER RULE 2 exactly as before.
         - UNKNOWN PRODUCT RULE still applies if the context has
           NO relevant explanatory content for that category.
         - Model routing (is_simple_query) is UNCHANGED — this
           fix makes the answer correct and consistent regardless
           of which model handles it, which is lower-risk than
           retuning the simple/complex heuristic and directly
           addresses "same answer in the KB, different outcome
           depending on phrasing".
         - MUST be tested against BOTH phrasings from the repro
           pair above (one routes gpt-4o-mini, one routes gpt-4.1)
           to confirm consistent, correct answers from both models.

         PART 2 — Bereavement-specific handoff number
         (companion to supervisor.py v1.6.0)

         BACKGROUND: see supervisor.py v1.6.0 changelog. The
         design doc describes the bereavement number
         (0370 850 2179) as "injected separately by the user
         prompt builder when bereavement-specific terms are
         detected" but this was never implemented — only the
         general number 0345 600 0371 existed anywhere in
         generator.py.

         FIX:
         - BEREAVEMENT_HANDOFF_NUMBER [NEW CONSTANT]: single
           source of truth for "0370 850 2179", referenced from
           both SYSTEM_PROMPT and build_user_prompt() so they
           cannot drift apart (same pattern as
           UNKNOWN_PRODUCT_RESPONSE in v1.6.0).
         - SYSTEM PROMPT — HUMAN HANDOFF RULE [MODIFIED]: added a
           sentence noting that if the user prompt contains a
           NOTE specifying an alternative handoff number for this
           response, that number must be used INSTEAD of
           0345 600 0371 for the human handoff message ONLY — all
           other numbers in the response (financial disclaimer,
           unknown product rule, account access rule) are
           unaffected.
         - build_user_prompt() [MODIFIED]: when
           state.__dict__.get("_bereavement") is True (set by
           supervisor.py v1.6.0, now correctly propagated per
           graph.py v1.1.0), injects a bereavement_note
           instructing the model to use
           BEREAVEMENT_HANDOFF_NUMBER for the human handoff in
           this response only.
         - Placed alongside empathy_note: BEREAVEMENT_TRIGGERS is
           a strict subset of EMPATHY_TRIGGERS (supervisor.py
           v1.6.0), so _bereavement=True always implies
           state.needs_empathy=True and empathy_note is also
           present — the two notes work together (empathy framing
           + correct handoff number).

v1.8.0 — June 2026 | Mukesh Kund
         Three SYSTEM PROMPT fixes found during v1.7.0 testing
         (Seq 7/8: bereavement + cancer-payout queries)

         PART 1 — Unwanted "Sources:" URL block in responses
         [CITATION RULE MODIFIED]:
         - ROOT CAUSE: build_context() includes each chunk's
           source_url in the context sent to the model
           ("[1] Source: <title> (<url>)\\n<content>") so the
           model can identify [1]/[2]/[3]. The CITATION RULE told
           the model to use [1][2][3] inline markers (which
           extract_citations() turns into the clickable source
           chips shown below the response) but said nothing about
           NOT also reproducing those URLs as text.
         - Reproduced live (Seq 7/8): gpt-4.1 added its own
           "Sources:" section listing all 3 raw URLs as plain
           text, duplicating the citation chips already rendered
           by the UI. gpt-4o-mini (ISA/pension queries, Seq 3/4)
           did not do this — inconsistent output format depending
           on which model handled the query.
         - FIX: CITATION RULE now explicitly says do NOT add a
           "Sources:"/"Source:" section or otherwise repeat
           source URLs as text — [1][2][3] markers are sufficient,
           the interface renders source links separately. The URL
           shown in brackets after each source label in the
           context is for the model's reference only (to map
           [n] to the right source), never to be reproduced.

         PART 2 — Citation markers attached to the wrong sentence
         [CITATION RULE MODIFIED]:
         - Reproduced live (Seq 8 — "My mum is dying of cancer...
           how much money will she get"): the response's [1][2][3]
           markers were all attached to the sentence "This
           information is not available in the context provided
           and cannot be confirmed without access to her policy
           details" — i.e. markers were placed on the sentence
           saying the sources DON'T cover this, rather than on
           the earlier sentences whose general claims (when a
           policy pays out, what terminal illness cover means)
           those sources DO support.
         - FIX: CITATION RULE now explicitly says citation markers
           must only be attached to sentences containing a claim
           drawn from the provided context — never to sentences
           stating information is unavailable, cannot be
           confirmed, or directing the customer to contact Royal
           London/an adviser (those are not claims from the
           sources and must carry no [n] markers).

         PART 3 — Bereavement handoff number bled into the next,
         unrelated empathy response [HUMAN HANDOFF RULE MODIFIED]:
         - Reproduced live: Seq 7 ("My wife passed away last
           week...") correctly got the bereavement number
           (0370 850 2179, via v1.7.0's bereavement_note). Seq 8,
           the VERY NEXT message ("My mum is DYING of cancer... how
           much will she get") correctly got
           state.__dict__["_bereavement"]=False (mum is still
           alive — this is a hypothetical, not a bereavement) and
           so received NO bereavement_note — yet its response also
           said "0370 850 2179". Root cause: build_user_prompt()
           includes the last 6 conversation_history turns,
           including Seq 7's full response text containing
           "0370 850 2179" — gpt-4.1 reused that number from
           history "for consistency" despite this turn's NOTE not
           authorising it.
         - FIX: HUMAN HANDOFF RULE now explicitly states that the
           handoff number for THIS response must be decided
           independently of any phone numbers appearing in the
           conversation history above — if this response's NOTE
           does not specify an alternative number, use
           0345 600 0371 even if a different number appeared in a
           previous assistant turn.

         NONE of these three changes alter is_simple_query,
         model routing, retrieval, needs_empathy/needs_disclaimer/
         _bereavement/_override_triggered detection, or
         build_context()'s output — all SYSTEM_PROMPT-only,
         additive to existing rules. cache_check.py, supervisor.py
         and graph.py are unchanged in this round.

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


# ── Empathy / Financial Disclaimer Detection ──────────────────
# MOVED to supervisor.py in v1.6.0 (EMPATHY_TRIGGERS,
# FINANCIAL_DECISION_TRIGGERS, needs_empathy(), needs_disclaimer()).
# supervisor_node() now sets state.needs_empathy and
# state.needs_disclaimer BEFORE cache_check runs, so the semantic
# cache can be skipped for sensitive disclosures. generator_node()
# below only READS these flags (state.needs_empathy,
# state.needs_disclaimer, state.is_sensitive). See supervisor.py
# v1.5.0 changelog for full rationale.


# ── Unknown Product Rule Response ─────────────────────────────
# Single source of truth for the UNKNOWN PRODUCT RULE refusal
# text (v1.6.0). Used in SYSTEM_PROMPT (so GPT reproduces it
# verbatim) AND checked in generator_node() after generation — if
# GPT returns this text, it is treated as a refusal
# (refusal_triggered=True), which makes cache_write_node skip
# caching it via its existing
# "refusal_triggered or not final_response" check. Prevents a
# wrong "no information" answer for one phrasing from being
# cached and then replayed for other phrasings of the same topic.
UNKNOWN_PRODUCT_RESPONSE = (
    "I'm sorry, I don't have information about that in our "
    "knowledge base. For assistance please contact Royal London "
    "directly on 0345 600 0371 Monday to Friday 8am to 6pm."
)

# ── Bereavement Handoff Number ────────────────────────────────
# NEW in v1.7.0 — single source of truth for the
# bereavement-specific support line, referenced from both
# SYSTEM_PROMPT (HUMAN HANDOFF RULE) and build_user_prompt()'s
# bereavement_note so they cannot drift apart. See supervisor.py
# v1.6.0 / generator.py v1.7.0 changelogs.
BEREAVEMENT_HANDOFF_NUMBER = "0370 850 2179"

# ── System Prompt ─────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a helpful and professional \
customer service assistant for RLG (a UK insurance and \
pensions provider).

RESPONSE LENGTH RULE:
Keep responses concise and mobile-friendly.
Maximum 300 words. Use bullet points for lists.
No unnecessary repetition or padding.

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


# ── Helper functions ──────────────────────────────────────────
# needs_empathy() and needs_disclaimer() MOVED to supervisor.py
# in v1.6.0 — see SYSTEM_PROMPT note above and supervisor.py
# v1.5.0 changelog.


def is_simple_query(query: str, is_sensitive: bool) -> bool:
    """
    Route to gpt-4o-mini (True) or gpt-4.1 (False).

    is_sensitive is state.is_sensitive (== state.needs_empathy),
    set by supervisor.py BEFORE this node runs — replaces the
    previous local needs_empathy(query) call (v1.6.0).
    """
    query_lower = query.lower()
    complex_indicators = [
        "compare" in query_lower,
        "difference between" in query_lower,
        "explain" in query_lower,
        "calculate" in query_lower,
        len(query.split()) > 20,
        query.count("?") > 1,
        is_sensitive,
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

    # Bereavement note — NEW in v1.7.0. _bereavement is set by
    # supervisor.py v1.6.0 and is a strict subset of
    # needs_empathy, so when this is True, empathy_note above is
    # also present. Tells GPT to use the bereavement-specific
    # support line for the human handoff in this response only —
    # see SYSTEM_PROMPT HUMAN HANDOFF RULE.
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

    disclaimer_note = ""
    if state.needs_disclaimer:
        disclaimer_note = (
            "NOTE: This query involves a financial decision. "
            "Please add the financial disclaimer as plain text "
            "at the end. Do NOT wrap it in asterisks or any "
            "markdown formatting whatsoever.\n\n"
        )

    # Override note — injected when supervisor detected this query
    # as a contextual follow-up (frustration, disagreement,
    # clarification etc). Tells GPT explicitly to use the
    # conversation history rather than treating this as a new
    # product question and firing the UNKNOWN PRODUCT RULE.
    override_note = ""
    if state.__dict__.get("_override_triggered"):
        override_note = (
            "NOTE: The customer is referring to a previous "
            "exchange in the conversation above. You DO have "
            "access to the conversation history shown. "
            "Acknowledge what was previously discussed and "
            "respond based on that context. "
            "Do NOT say you have no information about this — "
            "use the conversation history to understand what "
            "they are asking about and respond helpfully. "
            "If they are expressing frustration, acknowledge "
            "it genuinely, clarify your previous answer, and "
            "offer further help or the phone number "
            "0345 600 0371.\n\n"
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

    # state.needs_empathy, state.needs_disclaimer and
    # state.is_sensitive are now set by supervisor.py (v1.5.0),
    # BEFORE cache_check runs — no longer computed here (v1.6.0).

    try:
        # Route: simple queries → gpt-4o-mini, complex → gpt-4.1
        deployment = (
            DEPLOYMENT_FAST
            if is_simple_query(state.query, state.is_sensitive)
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

        # ── UNKNOWN PRODUCT RULE refusal detection (v1.6.0) ──
        # If GPT returned the UNKNOWN PRODUCT RULE refusal text,
        # treat it as a refusal (refusal_triggered=True) so
        # cache_write_node's existing
        # "refusal_triggered or not final_response" check skips
        # caching it. Prevents a wrong "no information" answer for
        # one phrasing from being cached and then replayed for
        # other phrasings of the same topic.
        if UNKNOWN_PRODUCT_RESPONSE in updated_text:
            state.refusal_triggered = True
            state.raw_response      = UNKNOWN_PRODUCT_RESPONSE
            state.final_response    = UNKNOWN_PRODUCT_RESPONSE
            state.citations         = []
        else:
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
            citations=len(state.citations),
            latency_ms=round(latency),
            empathy=state.needs_empathy,
            disclaimer=state.needs_disclaimer,
            refusal_triggered=state.refusal_triggered,
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