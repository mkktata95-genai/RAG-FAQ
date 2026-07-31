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

v1.1.0 — July 2026 | Mukesh Kund
         URL-in-body rule added to SYSTEM_PROMPT (initial attempt)

         SUPERSEDED BY v1.2.0 — this entry kept for history only.
         Added URL IN BODY RULE to SYSTEM_PROMPT with a carve-out
         for "handoff URLs at the END of the response". This proved
         insufficient — LLM still wrote URLs in handoff sentences
         and financial disclaimers. Full solution in v1.2.0.

v1.2.0 — July 2026 | Mukesh Kund
         Full URL + email purge from SYSTEM_PROMPT + static citation
         constants defined

         PROBLEM (sprint 1 testing, 13 July):
         - v1.1.0's handoff URL exception was insufficient: LLM
           still wrote royallondon.com URLs in bereavement handoffs,
           financial disclaimers, and ACCOUNT ACCESS RULE responses.
         - RECOMMENDATION_RESPONSE and UNKNOWN_PRODUCT_RESPONSE
           Python constants also contained raw URLs — bypassed the
           SYSTEM_PROMPT rule entirely (returned directly by Python).
         - When no URL appeared inline, customer had no clickable
           link to the contact/adviser page — citation pill was
           missing entirely.

         FIX 1 — SYSTEM_PROMPT rules fully rewritten (no URL exceptions):
         - HUMAN HANDOFF RULE: "please see our bereavement support
           page below" / "see the link in our resources below"
         - FINANCIAL DISCLAIMER RULE: "see the link in our resources
           below" — no URL
         - PHONE NUMBER RULE: no URLs at all; explicit rule added
           that email addresses must never appear in responses
         - CITATION RULE: added count constraint — "only cite [n]
           numbers that correspond to sources in the context; if 1
           source, only [1] may be used"
         - URL IN BODY RULE: extended to cover email addresses
         - ACCOUNT ACCESS RULE: hardcoded URL removed from refusal
         - ANSWER RULES / ORGANISATION BLOCKLIST: URL refs removed

         FIX 2 — Static citation dicts defined (NEW CONSTANTS):
         - CONTACT_CITATION, ADVISER_CITATION, BEREAVEMENT_CITATION
         - Single source of truth for the three Royal London pages
           that get injected as Citation pills by generator_node
           (v2.2.0) whenever a response redirects the customer.
         - Defined here so prompt_builder_node and generator share
           them without circular imports.

         FIX 3 — Python constants URL-stripped:
         - UNKNOWN_PRODUCT_RESPONSE: "please see our contact page"
         - RECOMMENDATION_RESPONSE: trailing URL lines removed

         FIX 4 — bereavement_note URL removed:
         - Was: injected BEREAVEMENT_HANDOFF_URL into user prompt
         - Now: model told "please see our bereavement support page
           below" — BEREAVEMENT_CITATION pill injected by
           generator_node v2.2.0 instead.

v1.3.0 — July 2026 | Mukesh Kund
         bereavement_note inline comment updated

         Minor: inline comment on bereavement_note block updated
         to reflect v1.2.0's approach (URL removed, Citation pill
         injected by generator). No logic change.

v1.4.0 — July 2026 | Mukesh Kund
         BUG #3 FIX — Conversation history sanitisation

         PROBLEM: raw client-supplied conversation_history was
         injected into the prompt verbatim. A refused/harmful turn
         earlier in the session got re-sent to Azure OpenAI on
         every later legitimate query, re-tripping the Content
         Safety filter and forcing generator.py's
         retry-without-history recovery path (extra LLM call +
         latency) even though the current query was clean. Two
         prior fixes (v1.7.0/v1.9.0 in the old generator.py) only
         told the model to behave despite toxic history via
         SYSTEM_PROMPT instructions — they didn't remove the toxic
         text itself.

         FIX: new sanitize_history() — drops any Assistant turn
         whose content matches a known REFUSAL_TEMPLATES string,
         plus the User turn immediately before it (the trigger).
         Runs BEFORE the [-6:] window slice in both the main
         history block and override_note, so refused turns can't
         consume window space or get anchored to as "the last
         question". Root-cause fix — eliminates the extra
         LLM call/retry rather than recovering from it after
         the fact.

         No new state field or client contract change required —
         detection is purely content-based against the existing
         fixed refusal strings.

v1.5.0 — July 2026 | Mukesh Kund
         BUG #19 FIX — RECOMMENDATION RULE over-blocking neutral
         comparisons ("X vs Y")

         PROBLEM: confirmed live — "Difference between ISA and
         Income Protection?" and other neutral "X vs Y" product-
         type comparisons were refused with RECOMMENDATION_RESPONSE
         ("As an AI assistant I'm not able to make personal
         financial recommendations..."), then that bad refusal got
         served from cache to further users. Root cause: the
         RECOMMENDATION RULE's examples were all personal-framing
         phrases ("what's best for me?", "which should I choose?"),
         but the rule itself had no explicit boundary — the model
         was free to (and did) generalise it to any comparison-
         shaped query, personal framing or not. Not a
         RECOMMENDATION_TRIGGERS keyword-list bug (that list is
         separate, only controls cache-bypass) — this is a prompt-
         instruction gap, since RECOMMENDATION_RESPONSE is applied
         by the LLM's own judgement per the SYSTEM_PROMPT rule, not
         by a code-level check.

         FIX: added an explicit carve-out to RECOMMENDATION RULE —
         neutral factual comparisons between product TYPES (no
         personal framing) must be answered normally under the
         ANSWER RULES; the rule only fires when the customer asks
         Aria to choose/judge FOR them ("for me", "should I",
         "which one should I", naming their own circumstances).
         Explicitly states "vs"/"compare" alone is not personal
         framing. Kept both positive (refuse) and negative (answer
         normally) examples side by side so the boundary is
         unambiguous rather than left to the model's generalisation.

         Does NOT touch RECOMMENDATION_TRIGGERS / is_recommendation_
         query() in supervisor.py — that list still correctly
         bypasses the cache for genuine personal-recommendation
         phrasing and is unaffected by this change.

         ROLLBACK: revert to v1.4.0's RECOMMENDATION RULE text
         (personal-framing examples only, no explicit carve-out).

v1.6.0 — July 2026 | Mukesh Kund
         BUG #27 + #28 FIX — two rule responses reference "the
         link/contact page" with no citation actually attached

         #27 — ACCOUNT ACCESS RULE. Response text was hardcoded
         inline in SYSTEM_PROMPT with no matching Python constant,
         so generator.py had nothing to detect and never attached
         CONTACT_CITATION, despite this file's own CONTACT_CITATION
         header comment already documenting ACCOUNT ACCESS RULE as
         one of the three rules meant to get the pill. Added
         ACCOUNT_ACCESS_RESPONSE constant (single source of truth,
         same pattern as UNKNOWN_PRODUCT_RESPONSE), referenced via
         f-string instead of a hardcoded duplicate.

         #28 — FINANCIAL DISCLAIMER RULE. The disclaimer paragraph
         is an LLM judgement call, entirely separate from
         state.needs_disclaimer (set by FINANCIAL_DECISION_TRIGGERS
         keyword matching in supervisor.py). Confirmed live: LLM
         wrote the disclaimer on a query ("...as a gig economy
         worker?") that didn't match any trigger keyword, so
         needs_disclaimer stayed False and ADVISER_CITATION was
         never attached — the disclaimer's own "please see the
         link in our resources below" had no link behind it. Added
         FINANCIAL_DISCLAIMER_TEXT constant, same pattern.

         Both now detected directly in generator.py's post-
         generation text (see that file's changelog) rather than
         inferred from upstream flags — closes the gap for any
         case the LLM judges correctly but a keyword list didn't
         predict in advance, not just the ones on file.

         ROLLBACK: revert to v1.5.0 — restore hardcoded quoted
         text inline for both rules, remove ACCOUNT_ACCESS_RESPONSE
         and FINANCIAL_DISCLAIMER_TEXT constants.

═══════════════════════════════════════════════════════════════
"""

import os
import time
import structlog
from dotenv import load_dotenv, find_dotenv

from core.schemas import AgentState
from core.refusal import REFUSAL_TEMPLATES

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
    "knowledge base. For assistance please see our contact page."
)

# BUG #27 FIX (July 2026, Mukesh Kund): same single-source-of-truth
# pattern as UNKNOWN_PRODUCT_RESPONSE above. Previously the ACCOUNT
# ACCESS RULE's exact response text was hardcoded directly inline
# in SYSTEM_PROMPT with no matching Python constant and no detection
# in generator.py — so despite the response text saying "please use
# our contact page", no CONTACT_CITATION pill was ever attached
# (confirmed live: citations=0 on account-access queries). The
# header comment on CONTACT_CITATION below already documented this
# rule as one that should get the pill; the wiring was just missing.
ACCOUNT_ACCESS_RESPONSE = (
    "I'm not able to access account information directly. "
    "For your account details please use our contact page "
    "or log in to your Royal London account."
)

# BUG #28 FIX (July 2026, Mukesh Kund): the FINANCIAL DISCLAIMER
# RULE is a judgement call the LLM makes itself (query "involves a
# financial decision, investment choice, or personal financial
# advice") — entirely separate from state.needs_disclaimer, which
# is set by FINANCIAL_DECISION_TRIGGERS keyword matching in
# supervisor.py. The two can (and did — confirmed live) disagree:
# LLM wrote the disclaimer text (correctly judged this needed one)
# on a query ("...as a gig economy worker?") that didn't match any
# FINANCIAL_DECISION_TRIGGERS phrase, so needs_disclaimer stayed
# False and ADVISER_CITATION was never attached — leaving the
# disclaimer's own "please see the link in our resources below"
# with no link behind it. Same single-source-of-truth pattern as
# ACCOUNT_ACCESS_RESPONSE: generator.py now detects this exact text
# and attaches ADVISER_CITATION whenever it's actually present,
# regardless of the needs_disclaimer flag — closes the gap for any
# query the LLM judges as financial-advice-adjacent, not just the
# ones a keyword list happens to predict in advance.
FINANCIAL_DISCLAIMER_TEXT = (
    "Please note: This information is for general guidance only "
    "and does not constitute financial advice. For advice tailored "
    "to your personal circumstances, we recommend speaking with a "
    "qualified financial adviser — please see the link in our "
    "resources below."
)

# ── Static Contact Citation ───────────────────────────────────
# Injected as a Citation pill whenever a response redirects the
# customer to Royal London's contact page (UNKNOWN PRODUCT RULE,
# RECOMMENDATION RULE, ACCOUNT ACCESS RULE). Ensures the customer
# always sees a clickable link — no raw URLs in response body.
CONTACT_CITATION = {
    "url":     "https://www.royallondon.com/existing-customers/contact-us/",
    "title":   "Contact Royal London",
    "section": "Existing customers",
}

# ── Static Adviser Citation ───────────────────────────────────
# Injected when response includes a financial disclaimer or
# recommendation refusal directing customer to find an adviser.
ADVISER_CITATION = {
    "url":     "https://www.royallondon.com/find-a-financial-adviser/",
    "title":   "Find a financial adviser",
    "section": "Financial advice",
}

# ── Static Bereavement Citation ───────────────────────────────
# Injected for bereavement handoff — replaces the inline URL
# that was previously written into the response body by the LLM.
BEREAVEMENT_CITATION = {
    "url":     "https://www.royallondon.com/existing-customers/help-and-support/make-a-claim/tell-us-about-a-bereavement/",
    "title":   "Tell us about a bereavement",
    "section": "Make a claim",
}

# ── Bereavement Handoff URL ───────────────────────────────────
# v1.1.0: was a phone number (0370 850 2179).
# v1.2.0 (July 2026): changed to the bereavement page URL.
# REASON: Royal London's bereavement page has a dropdown selector
# where the customer chooses their policy type (pre-2004 savings,
# critical illness, Aegon Protection etc) and the correct contact
# number for THEIR policy is then displayed. There is no single
# bereavement number — giving any hardcoded number risks sending
# the customer to the wrong department in an already distressing
# situation. The URL is always correct regardless of policy type.
BEREAVEMENT_HANDOFF_URL = (
    "royallondon.com/existing-customers/help-and-support/"
    "make-a-claim/tell-us-about-a-bereavement/"
)

# Keep old name as alias so generator.py imports don't break
BEREAVEMENT_HANDOFF_NUMBER = BEREAVEMENT_HANDOFF_URL

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
    "ISAs, or other Royal London products."
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
If the customer asks Aria to recommend, suggest, or choose
between financial products FOR THEM PERSONALLY (for example
"what do you recommend?", "which pension should I choose?",
"what's best for me?", "what would you suggest?", "which is
better for me?", "should I switch to X?") — do NOT make a
recommendation.
Respond with exactly:
"{RECOMMENDATION_RESPONSE}"

Do NOT apply this rule to neutral factual comparisons between
product TYPES with no personal framing (for example "ISA vs
income protection", "pensions vs ISA", "difference between X
and Y", "compare X and Y", "what's the difference between a
SIPP and a workplace pension"). These are ordinary factual
questions — answer them normally under the ANSWER RULES below,
explaining what each product is and how they differ. The word
"vs" or "compare" alone is NOT personal framing and does not
trigger this rule by itself. Only refuse when the customer is
asking Aria to make the choice or judgement FOR them (e.g. adds
"for me", "should I", "which one should I", "would you", or
names their own circumstances).

This is a regulatory boundary — Aria is not a financial adviser.
When it applies, this rule takes priority over the UNKNOWN
PRODUCT RULE and all ANSWER RULES below.

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
ONLY add a handoff when the customer has mentioned one of the
genuine distress situations listed in EMPATHY RULE above
(terminal illness, bereavement, redundancy etc).

When applicable, end with one of the following (choose based
on the situation):

For BEREAVEMENT queries (someone has died):
"For support with your bereavement, please see our
bereavement support page where you can select your policy
type to find the right contact details and notify Royal
London online."

For other SENSITIVE queries (terminal illness, critical
illness, redundancy, financial hardship, serious medical):
"For personalised support tailored to your circumstances,
we recommend speaking with a qualified financial adviser.
Please see the link in our resources below."

Do NOT add handoff for standard administrative queries
(lost pension, making a claim, transfers, product questions).
NEVER write a URL in the handoff sentence — the interface
renders clickable links automatically from the citations.

FINANCIAL DISCLAIMER RULE:
ONLY add this disclaimer when the query involves a financial
decision, investment choice, or personal financial advice.
Write it as plain text with NO asterisks or markdown:
{FINANCIAL_DISCLAIMER_TEXT}
NEVER write a URL in the disclaimer text.

PHONE NUMBER RULE:
Royal London has different contact numbers for different policy
types and departments. NEVER hardcode a phone number in a
human handoff or anywhere in the body of a response unless:
(a) The customer explicitly asked for a contact number, AND
(b) That specific number appears in the retrieved context.
When providing general contact guidance, DO NOT write any URL
— the interface renders contact and adviser links automatically
as clickable pills. Simply refer to "our contact page" or
"our resources below".
If the customer asks for a specific contact number (e.g. for
funeral plans, Aegon policies, pre-2004 pensions), quote the
number from the retrieved context and cite the source [n].
Never invent or guess a phone number.
NEVER include email addresses in responses — direct customers
to the contact page link rendered by the interface instead.
Only use an email address if it appears in the retrieved
context AND the customer explicitly asked for an email contact,
and in that case it must be cited with a [n] marker from the
source page — never written as a bare address.

CITATION RULE:
Always cite sources sequentially starting from [1].
Use [1] for the first source you reference,
[2] for the second, [3] for the third.
Never skip numbers or use numbers out of order.
CRITICAL — only cite [n] numbers that correspond to
sources actually listed in the numbered context below.
If the context contains 1 source, only [1] may be used.
If 2 sources, only [1] and [2]. Never write [2] or [3]
if no second or third source appears in the context.
Only attach a [1][2][3] marker to a sentence that
contains a claim drawn directly from the provided
context. Do NOT attach citation markers to a sentence
that says information is unavailable, cannot be
confirmed, or that directs the customer to contact
Royal London or speak to an adviser — those sentences
are not claims from the sources and must carry no [n]
markers.
Do NOT add a "Sources:" or "Source:" section or repeat
source links anywhere in your response. The [1][2][3]
markers are sufficient — the interface displays source
links and contact/adviser links automatically as
clickable pills. The URL shown in brackets after each
source label in the context is for your reference only
to map [n] to the correct source — never reproduce it.
URL IN BODY RULE:
NEVER write any URL, web address, or email address
anywhere in your response text — not in the body, not
in handoffs, not in disclaimers. This includes all
royallondon.com URLs, adviser finder links, contact
page links, and email addresses. The interface renders
all links automatically as clickable pills. Simply
refer to "our contact page", "our resources below",
or "the link below" — never write the actual address.
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
Royal London's products, direct the customer to our
contact page (the link is rendered automatically below).

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
"{ACCOUNT_ACCESS_RESPONSE}"
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
   and direct the customer to our contact page

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


# ── History sanitisation (v1.4.0 — bug #3) ────────────────────
# Every refusal in the pipeline (input_safety, output_safety,
# retriever no-results, generator fallback) returns one of these
# fixed strings via get_refusal(). That makes them a reliable
# signature for "this turn was refused/harmful" — no new state
# flag or client-side metadata required.
_REFUSAL_TEXTS = {text.strip() for text in REFUSAL_TEMPLATES.values()}


def sanitize_history(conversation_history: list[dict]) -> list[dict]:
    """
    Strip refused/harmful turns out of conversation_history BEFORE
    they're ever injected into a prompt.

    Root-cause fix for bug #3: previously the raw client-supplied
    history was injected verbatim, so a harmful/jailbreak turn
    sitting earlier in the session got re-sent to Azure OpenAI on
    every subsequent legitimate query — tripping the Content Safety
    filter again and forcing the retry-without-history recovery
    path (extra LLM call + latency) on a completely clean query.

    Detection: an Assistant turn whose content matches a known
    REFUSAL_TEMPLATES string means the pipeline refused that
    exchange. Both that Assistant turn AND the User turn
    immediately before it (the actual trigger) are dropped.

    Any other Assistant content (real answers) is left untouched.
    """
    if not conversation_history:
        return conversation_history

    drop = set()
    for idx, turn in enumerate(conversation_history):
        role    = (turn.get("role") or "").lower()
        content = (turn.get("content") or "").strip()
        if role == "assistant" and content in _REFUSAL_TEXTS:
            drop.add(idx)
            if (
                idx > 0
                and (conversation_history[idx - 1].get("role") or "").lower()
                == "user"
            ):
                drop.add(idx - 1)

    if not drop:
        return conversation_history

    return [
        turn for i, turn in enumerate(conversation_history) if i not in drop
    ]


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

    # Conversation history — sanitised, then last 6 turns.
    # Sanitisation runs BEFORE the [-6:] slice so a run of refused
    # turns can't push legitimate history out of the window.
    history = ""
    clean_history = sanitize_history(state.conversation_history)
    if clean_history:
        recent = clean_history[-6:]
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

    # Bereavement note (v1.3.0 — URL removed from note).
    # The bereavement URL is now injected by generator_node as a
    # static Citation pill (BEREAVEMENT_CITATION). The model is
    # told to refer to "our bereavement support page" only —
    # no URL written in the response body.
    bereavement_note = ""
    if state.__dict__.get("_bereavement"):
        bereavement_note = (
            "NOTE: This query relates to a bereavement. For the "
            "HUMAN HANDOFF RULE in this response, refer the "
            "customer to our bereavement support page using the "
            "phrase 'please see our bereavement support page below'. "
            "Do NOT write any URL or phone number — the interface "
            "renders the link automatically. The correct contact "
            "number depends on the customer's policy type and is "
            "shown on the bereavement page after they select their "
            "policy type.\n\n"
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
        if clean_history:
            recent = clean_history[-6:]
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
                "royallondon.com/existing-customers/contact-us/\n\n"
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
            history_turns_dropped=(
                len(state.conversation_history)
                - len(sanitize_history(state.conversation_history))
                if state.conversation_history else 0
            ),
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