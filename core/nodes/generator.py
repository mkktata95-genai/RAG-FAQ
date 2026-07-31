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

v1.9.0 — June 2026 | Mukesh Kund
         Override note anchored to the immediately preceding query
         (Bug 10)

         ROOT CAUSE:
         - build_user_prompt() includes the last 6 conversation
           history turns (formatted as "User: .../Assistant: ..."
           chronologically). When _override_triggered=True, the
           override_note told the model to "acknowledge what was
           previously discussed and respond based on that context"
           — but gave no guidance on WHICH previous turn to anchor
           to.
         - Live repro (request_id=09d8c15-...):
           Conversation history contained (in order):
             Q1: My wife passed away (bereavement)
             Q2: My mum is dying of cancer (payout)
             Q3: What is an ISA?  <-- immediately preceding
             Q4: Why didn't you answer my previous question?
           The model anchored to Q2 ("Your question about the
           payout from your mum's policy is important") — the most
           emotionally salient topic in history — rather than Q3
           (ISA), which was the actual most recent user question
           and the natural referent of "my previous question".

         FIX (build_user_prompt() only):
         - When building the override_note, the last User turn from
           conversation_history[-6:] is now extracted and injected
           explicitly:
             "The customer's PREVIOUS question (the one immediately
             before this message) was: '<last user question>'"
           This pins the model to the correct referent regardless
           of how many emotionally prominent turns appear earlier
           in history.
         - Falls back gracefully: if conversation_history is empty
           or no User turn is found, override_note stays "" and the
           pipeline continues normally.
         - No SYSTEM_PROMPT changes, no routing changes.
         - Companion change: retriever.py v1.1.0 now skips Azure
           Search entirely for _override_triggered=True queries,
           so state.retrieved_chunks is always [] when override
           is active. build_user_prompt() now omits the context
           block and its closing instruction entirely when chunks
           are empty AND override is active — preventing an empty
           "Context from RLG documentation: (nothing)" block
           being sent to the model, and ensuring it relies solely
           on conversation history as intended.

v2.0.0 — July 2026 | Mukesh Kund
         Sprint 1 refactor — slim generator + dotenv fix

         FUNCTIONS MOVED TO prompt_builder_node.py (core/nodes/):
         - SYSTEM_PROMPT
         - UNKNOWN_PRODUCT_RESPONSE (still imported from here
           for detection — single source of truth stays in
           prompt_builder_node.py)
         - BEREAVEMENT_HANDOFF_NUMBER (same — imported from
           prompt_builder_node.py)
         - build_context()
         - build_user_prompt()

         WHAT STAYS IN GENERATOR:
         - is_simple_query() — model routing logic
         - generator_node() — the actual LLM call
         - extract_citations() — citation renumbering
         - clean_response_text() — post-processing
         - get_openai_client() — singleton Azure client

         GENERATOR CHANGE — reads state.built_prompt:
         - Old: build_user_prompt(state) called here
         - New: state.built_prompt (set by prompt_builder_node)
         - prompt_builder_node runs BEFORE generator in the
           pipeline; state.built_prompt is always set by then.
         - Defensive fallback: if built_prompt is empty
           (shouldn't happen but defended against), generator
           falls back to a minimal safe prompt rather than
           crashing. This preserves the FCA-regulated response
           path under all conditions.

         is_simple_query() ENHANCEMENT — query_type signal:
         - Added query_type parameter (from state.query_type,
           set by classifier_node).
         - BROAD queries are NEVER routed to gpt-4o-mini
           regardless of word count. They always get gpt-4o
           (DEPLOYMENT_FAST) minimum.
         - WHY: "What types of pensions does Royal London offer?"
           is short (8 words) and would previously route to
           gpt-4o-mini via word-count heuristic, producing thin
           answers. BROAD queries by definition need comprehensive
           multi-product coverage — gpt-4o consistently delivers
           richer, FCA-disclaimer-consistent responses for these.
         - SPECIFIC queries: existing word-count + keyword
           heuristic unchanged.

         DOTENV FIX:
         - was: load_dotenv() — no args, no override
         - now: load_dotenv(find_dotenv(usecwd=False), override=True)

v2.1.0 — July 2026 | Mukesh Kund
         Four production fixes

         FIX 1 — "should i" removed from simple_indicators
         (is_simple_query):
         - "should i" was in the simple_indicators keyword list,
           routing queries like "should i transfer my pension?" to
           DEPLOYMENT_FAST (gpt-4o). However supervisor.py's
           FINANCIAL_DECISION_TRIGGERS includes "should i" —
           meaning the query gets needs_disclaimer=True (correct)
           but also routes to the weaker model (wrong). For any
           FCA-regulated query needing a disclaimer, DEPLOYMENT_MAIN
           must handle it. Removed "should i" from simple_indicators.

         FIX 2 — Dynamic max_tokens by query_type:
         - Was hardcoded at 800. BROAD queries (pension type
           overviews, ISA comparisons) were being truncated
           mid-answer at 800 tokens.
         - Now: max_tokens = 1200 for BROAD, 800 for SPECIFIC.
         - 1200 gives sufficient headroom for a comprehensive
           multi-type overview while staying well within the
           model's context window and cost budget.

         FIX 3 — extract_citations() duplicate index guard:
         - Added seen_indices set alongside existing seen_urls set.
         - Guards against the same retrieved_chunks index appearing
           under two different original citation numbers (e.g. model
           writes [1] and [3] both referring to the same chunk).
           Without this, two Citation objects could be created for
           the same chunk — one would have wrong index number and
           pill labels could appear out of order in the UI.

         FIX 4 — clean_response_text() expanded:
         - Now also strips markdown horizontal rules (--- *** ___)
           that gpt-4.1 occasionally emits between sections.
         - Strips trailing whitespace per line.
         - Collapses 3+ consecutive blank lines to max 2.
         - These artefacts were rendering as blank gaps / horizontal
           lines inside the demo.html bubble markdown renderer.

v2.2.0 — July 2026 | Mukesh Kund
         Static citation injection + URL purge from all response text

         PROBLEM (observed sprint 1 testing, 13 July):
         - URLs appearing inline in response body (LLM-generated and
           Python constants): RECOMMENDATION_RESPONSE, UNKNOWN_PRODUCT_
           RESPONSE, bereavement handoff, financial disclaimer, ACCOUNT
           ACCESS RULE all contained raw royallondon.com URLs.
         - When the LLM redirects the customer to an adviser or contact
           page, no citation pill appeared in the UI — the customer only
           saw raw text with no clickable link.

         FIX — make_static_citations() [NEW FUNCTION]:
         - Builds Citation pill objects from CONTACT_CITATION,
           ADVISER_CITATION, BEREAVEMENT_CITATION (defined in
           prompt_builder_node.py v1.2.0).
         - Deduplicates by URL, numbering sequentially after any
           existing retrieved-source citations.
         - Called by generator_node after extract_citations():
             Bereavement → BEREAVEMENT_CITATION + CONTACT_CITATION
             Financial disclaimer → ADVISER_CITATION
             Other sensitive (empathy) → ADVISER_CITATION + CONTACT_CITATION
             UNKNOWN PRODUCT refusal → CONTACT_CITATION
         - All Python constants (UNKNOWN_PRODUCT_RESPONSE,
           RECOMMENDATION_RESPONSE) now have URLs stripped — text
           refers to "our contact page" / "our resources below".
         - SYSTEM_PROMPT rules rewritten: HUMAN HANDOFF, FINANCIAL
           DISCLAIMER, PHONE NUMBER, CITATION, ACCOUNT ACCESS — all
           URLs removed. LLM instructed never to write any URL,
           web address, or email address in response text.
         - Email address handling: LLM instructed never to include
           email addresses; only quote from retrieved context if
           customer explicitly asked and source is verified.

         COMPANION CHANGES (same release, separate files):
         - refusal.py: NO_RESULTS and GENERAL templates had raw URLs
           stripped — now say "see our contact page below".
         - retriever.py: both refusal sites (no chunks + exception)
           now set state.citations = [CONTACT_CITATION pill] so the
           customer always sees a clickable link even on hard failures.
         - generator.py error handler: same CONTACT_CITATION injection.
         - prompt_builder_node.py v1.2.0: CONTACT_CITATION,
           ADVISER_CITATION, BEREAVEMENT_CITATION constants defined;
           RECOMMENDATION_RESPONSE URLs stripped; bereavement_note
           URL removed; all SYSTEM_PROMPT rules rewritten.
         - supervisor.py v1.9.0: RECOMMENDATION_TRIGGERS expanded from
           18 → 33 phrases; sufficiency/adequacy/comparison/projection
           patterns added (e.g. "will my pension be enough",
           "enough to retire", "am i on track", "better than").

v2.4.0 — July 2026 | Mukesh Kund
         GPT-5 compatibility + model-agnostic API call helper.

         WHAT CHANGED:
         - Added _build_create_kwargs(model, max_tokens, temperature)
           helper (same pattern as classifier_node.py v1.2.0,
           cache_check.py v1.6.0).
         - generator_node() streaming call now uses
           **_build_create_kwargs(deployment, max_tokens, 0.1)
           replacing hardcoded max_tokens / temperature kwargs.
         - GPT-4 family → max_tokens + temperature.
           GPT-5 family → max_completion_tokens, no temperature.
         - Dynamic max_tokens variable (BROAD=1200, SPECIFIC=800)
           is passed into helper unchanged — routing logic unaffected.
         - Backwards compatible: DEPLOYMENT_MAIN / DEPLOYMENT_FAST
           env var swap to any gpt-4* restores original behaviour.

         ROLLBACK: revert to v2.3.0 — restore max_tokens=max_tokens,
         temperature=0.1 directly in the streaming create() call.

v2.3.0 — July 2026 | Mukesh Kund
         True token streaming + KV cache observability

         FIX 1 — True OpenAI token streaming (stream=True):
         - Was: client.chat.completions.create() blocking call —
           full response assembled before returning to server.py,
           which then split on spaces with asyncio.sleep(0.02)
           (artificial word-by-word delay, ~2-4s added latency).
         - Now: stream=True — OpenAI returns chunk iterator
           immediately. Tokens collected into list as they arrive,
           generator_node remains synchronous (no async refactor
           needed — runs inside run_in_executor in server.py).
           state.stream_tokens populated with raw OpenAI chunks.
         - server.py v1.2.0 reads state.stream_tokens directly and
           yields each token to the SSE client without artificial
           delay — perceived latency drops to time-to-first-token
           (~500ms) instead of total generation time.
         - stream_options={"include_usage": True} added so usage
           data (prompt/completion tokens) still arrives on the
           final chunk — no separate non-streaming usage call needed.
         - schemas.py: stream_tokens: list[str] added to AgentState.
         - Fallback in server.py: if stream_tokens is empty for any
           reason, falls back to space-split of final_response.

         FIX 2 — KV cache observability (cached_tokens logging):
         - Azure OpenAI returns prompt_tokens_details.cached_tokens
           in usage when KV prefix caching is active for the
           SYSTEM_PROMPT prefix (~1200 tokens, identical across all
           requests). Previously we had no visibility of whether
           prefix caching was working.
         - Now: cached_tokens read from usage_data.prompt_tokens_
           details.cached_tokens (AttributeError guarded — field
           absent on older API versions).
         - Logged as kv_cache_hit (INFO) when > 0, kv_cache_miss
           (DEBUG) when 0. If consistently 0 in logs after
           deployment, prefix caching is not active on this
           deployment's API version (requires 2024-12-01-preview
           or later) — visible without asking the deployment team.
         - cached_tokens added to state.token_usage dict →
           visible in demo.html meta row token count.

v2.5.0 — July 2026 | Mukesh Kund
         Non-streaming fallback for gpt-5-mini / gpt-5.6-luna.

         ROOT CAUSE (confirmed via Microsoft Tech Community):
         - Azure OpenAI gpt-5-mini with stream=True returns
           empty choices [] — no delta tokens arrive on the
           SSE stream. Same issue affects gpt-5.6-luna which
           buffers the full completion as a single chunk.
         - Result: tokens=[], raw_response="", state.raw_response
           never set → "No response generated" in UI.
         - Root cause is Azure-side, not in our code.

         FIX — Non-streaming fallback:
         - After streaming loop, if raw_response.strip() is empty,
           retry the same call with stream=False.
         - Non-streaming call returns full response in a single
           choices[0].message.content — always works regardless
           of model streaming support.
         - stream_tokens set to [raw_response] as single chunk
           so server.py SSE path still works correctly.
         - stream_empty_fallback WARNING logged for observability.
         - Makes generator model-agnostic: any model assigned in
           .env works regardless of streaming support status.

         ROLLBACK:
         - Remove the fallback block (lines after raw_response join)
         - Restore: state.stream_tokens = tokens; state.model_used
         - Revert max_tokens to: 1200 if BROAD else 800

v2.6.0 — July 2026 | Mukesh Kund
         Content-filter-aware exception handling (Tier 1 item #23).

         ROOT CAUSE:
         - Azure OpenAI's built-in content filter (separate from
           and independent of our own Layers 1-5 in safety.py —
           this fires INSIDE the chat.completions.create() call
           itself, on every deployment, always-on) can reject a
           request for two genuinely different reasons that the
           old except Exception block treated identically:
             Case A — the CURRENT query is genuinely concerning
               (e.g. "complete the application for me" — a
               social-engineering attempt). Filter did its job.
             Case B — the current query is completely innocent,
               but TOXIC CONVERSATION HISTORY (e.g. a DAN jailbreak
               attempt several turns earlier) poisoned the prompt.
               Confirmed live: "Who is the PM of UK?" — itself
               harmless — was rejected with jailbreak:detected=True
               because prompt_builder_node.py's history block
               (conversation_history[-6:], unsanitised) still
               contained the raw jailbreak text from an earlier
               turn in the same session.
         - Both cases fell into the same generic except block and
           returned RefusalReason.GENERAL — a vague "I'm unable to
           process your request" message that reads like a bug to
           a legitimate customer wrongly caught by Case B.

         FIX — is_content_filter_error() [NEW HELPER] +
         generator_node() except block [MODIFIED]:
         - Detects Azure content-filter errors specifically
           (content_filter_result / content_filter in the error
           string) rather than falling into the fully generic
           handler. Parses out which category fired (jailbreak,
           violence, hate, self_harm, sexual) for observability —
           every time Azure's filter catches something our own
           Layers 1-4 regex missed is a concrete tuning signal.
         - If content_filter fires AND conversation_history is
           non-empty: retry ONCE with history stripped from
           built_prompt (regex strip of the "Previous conversation:
           ...\n\n" block prompt_builder_node.py prepends — see
           its v1.x docstring for the exact format this matches).
             Retry succeeds → serve that response normally, log
             history_contamination_recovered (INFO) — a genuinely
             useful metric for how often this occurs in practice.
             Retry also fails / no history to strip → treat as
             Case A, fall through to the refusal below.
         - Capped at exactly one retry — never loops, does not
           double latency/cost on every content-filter event.
         - Genuine Case A blocks now use RefusalReason.HARMFUL
           (existing template: "I'm unable to assist with that
           request...") instead of GENERAL — clearer, more
           honest tone for an actual boundary vs. a system error.
         - Non-content-filter exceptions (network errors, timeouts,
           etc.) are unaffected — still fall through to the
           original GENERAL refusal path exactly as before.
         - Does NOT fix the root architectural gap (unsanitised
           history injection in prompt_builder_node.py — tracked
           separately, bug #3 in the Tier 2 backlog). This is the
           pragmatic, immediate countermeasure; proper history
           sanitisation is a larger, separate piece of work.

         ROLLBACK:
         - Remove is_content_filter_error() and the retry-without-
           history block.
         - Restore the except block to its v2.5.0 form: log
           generator_error, set refusal_triggered=True,
           final_response=get_refusal(RefusalReason.GENERAL),
           citations=make_static_citations(CONTACT_CITATION).

v2.7.0 — July 2026 | Mukesh Kund
         BUG #12 FIX — Citation orphan (dangling [n] with no pill)

         PROBLEM: extract_citations() renumbered every marker in
         the response text FIRST (pure order-of-appearance), then
         built the citations list second, silently dropping entries
         whose source_url or chunk index had already been seen.
         Net effect: rendered text could contain e.g. "[2]" with no
         matching citation pill — whenever two cited indices pointed
         to chunks sharing the same source_url (different sections
         of one page), or the model hallucinated a citation number
         beyond len(retrieved_chunks).

         FIX: resolve/filter candidate citations FIRST (skip
         duplicate chunk index, duplicate source_url, out-of-range
         index), THEN assign final sequential numbers only to
         survivors. Orphan markers are removed from the text
         entirely (regex replace → "") instead of being left as a
         dangling number with nothing behind it.

         ROLLBACK: revert to v2.6.0's extract_citations() —
         renumber-then-filter instead of filter-then-renumber.

v2.8.0 — July 2026 | Mukesh Kund
         BUG #27 + #28 FIX — ACCOUNT ACCESS RULE and FINANCIAL
         DISCLAIMER RULE responses missing their citation pills

         #27: added ACCOUNT_ACCESS_RESPONSE import + detection
         branch mirroring UNKNOWN_PRODUCT_RESPONSE's — attaches
         CONTACT_CITATION whenever the ACCOUNT ACCESS RULE fires.

         #28: added FINANCIAL_DISCLAIMER_TEXT import. static_extras
         now checks `state.needs_disclaimer OR FINANCIAL_DISCLAIMER_
         TEXT in updated_text` instead of the flag alone — attaches
         ADVISER_CITATION (the dedicated /find-a-financial-adviser/
         page) whenever the LLM actually wrote the disclaimer
         paragraph, even on queries FINANCIAL_DECISION_TRIGGERS
         didn't predict in advance.

         Full rationale for both in prompt_builder_node.py v1.6.0
         changelog (both constants defined there, single source
         of truth).

         ROLLBACK: revert to v2.7.0 — remove ACCOUNT_ACCESS_RESPONSE
         and FINANCIAL_DISCLAIMER_TEXT from the import, remove the
         elif ACCOUNT_ACCESS_RESPONSE branch, restore `elif state.
         needs_disclaimer:` (drop the `or has_disclaimer_text`).

═══════════════════════════════════════════════════════════════
"""
import os
import re
import time
import structlog
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv, find_dotenv

from core.schemas import AgentState, Citation
from core.middleware import track_token_usage
# SYSTEM_PROMPT, UNKNOWN_PRODUCT_RESPONSE, BEREAVEMENT_HANDOFF_NUMBER
# moved to prompt_builder_node.py (v2.0.0). Imported from there —
# single source of truth, no duplication.
from core.nodes.prompt_builder_node import (
    SYSTEM_PROMPT,
    UNKNOWN_PRODUCT_RESPONSE,
    ACCOUNT_ACCESS_RESPONSE,
    FINANCIAL_DISCLAIMER_TEXT,
    BEREAVEMENT_HANDOFF_NUMBER,
    CONTACT_CITATION,
    ADVISER_CITATION,
    BEREAVEMENT_CITATION,
)

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
DEPLOYMENT_MAIN       = os.getenv("AZURE_OPENAI_DEPLOYMENT_MAIN", "gpt-4.1")
# v2.0.0: default corrected to gpt-4o (was gpt-4o-mini).
# DEPLOYMENT_FAST = gpt-4o throughout the pipeline per the
# finalised model assignment decision (FCA disclaimer consistency).
DEPLOYMENT_FAST       = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o")


# ── Model-agnostic API kwargs helper ─────────────────────────
def _build_create_kwargs(
    model: str,
    max_tokens: int,
    temperature: float | None = None,
) -> dict:
    """Return OpenAI completions.create() kwargs compatible with both
    GPT-4 and GPT-5 model families.

    GPT-4 family (gpt-4*): supports max_tokens and temperature.
    GPT-5 family (gpt-5*, o*, etc.): uses max_completion_tokens;
        temperature is not supported and must be omitted.

    The dynamic max_tokens value (BROAD=1200, SPECIFIC=800) is
    passed through unchanged — routing logic is unaffected.
    """
    is_gpt4 = "gpt-4" in model.lower()
    kwargs: dict = {}
    if is_gpt4:
        kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
    else:
        kwargs["max_completion_tokens"] = max_tokens
    return kwargs


# ── Content-filter handling (v2.6.0) ─────────────────────────
def is_content_filter_error(error_str: str) -> tuple[bool, str | None]:
    """
    Detect whether an exception from client.chat.completions.create()
    is Azure OpenAI's built-in content filter firing, and if so,
    which category triggered it (for observability only — the
    caller decides what to do regardless of category).

    This is Azure's infrastructure-level filter — separate from
    and independent of our own Layers 1-5 in safety.py. It cannot
    be disabled and fires inside every chat.completions.create()
    call, on every deployment.

    Returns (is_content_filter, category) where category is one
    of "jailbreak", "violence", "hate", "self_harm", "sexual", or
    None if the error is a content filter but the category could
    not be parsed from the error string.
    """
    if "content_filter_result" not in error_str and "content_filter" not in error_str:
        return False, None

    # Best-effort category parse against Azure's observed error
    # format, e.g.:
    #   'jailbreak': {'detected': True, 'filtered': True}
    #   'violence':  {'filtered': True, 'severity': 'medium'}
    # For logging/observability only — never used for routing
    # logic, so an unparsed category just logs as None.
    normalised = error_str.replace('"', "'")
    for category in ("jailbreak", "violence", "hate", "self_harm", "sexual"):
        match = re.search(
            rf"'{category}':\s*\{{[^}}]*?"
            rf"(?:'detected':\s*True|'severity':\s*'(?!safe)\w+')",
            normalised,
        )
        if match:
            return True, category

    return True, None  # content filter fired, category not parsed


def strip_history_from_prompt(built_prompt: str) -> str:
    """
    Remove the "Previous conversation:\\n...\\n\\n" block that
    prompt_builder_node.py prepends to built_prompt when
    conversation_history is non-empty.

    Used only as a one-off retry transformation when Azure's
    content filter rejects a call — never mutates state.built_prompt
    itself, and never touches prompt_builder_node.py. This is a
    pragmatic countermeasure for toxic-history false positives
    (see v2.6.0 changelog), not a fix for the underlying
    unsanitised-history-injection gap (tracked separately).

    Returns built_prompt unchanged if no history block is found
    at the start (e.g. conversation_history was already empty).
    """
    return re.sub(
        r"^Previous conversation:\n.*?\n\n",
        "",
        built_prompt,
        count=1,
        flags=re.DOTALL,
    )


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


# ── Model routing ─────────────────────────────────────────────
# TECHNICAL_COMPLEX_KEYWORDS — narrow list of regulated pension/tax
# terms that are genuinely non-obvious from structural signals alone.
# These are stable HMRC/FCA regulatory concepts, not arbitrary words.
# CPX-02 fix: "tapered annual allowance" → gpt-4.1
# CPX-05 fix: "tax-free lump sum" + "25% tax" → gpt-4.1
# Extended to cover other known complex regulatory areas.
#
# WHY a keyword list here (despite our general preference for
# signal-based routing): these terms have a near-zero false positive
# rate for genuine complexity and a near-100% recall for queries that
# actually require the precision of gpt-4.1. For example, "tapered
# annual allowance" will never appear in a simple FAQ query — it only
# appears when the customer is asking about a specific HMRC taper rule
# that affects high earners, which genuinely needs careful handling.
# The list is deliberately narrow so maintenance burden is minimal.
#
# MIGRATION NOTE: When upgrading to GPT-5 (required before Oct 2026
# when gpt-4o and gpt-4.1 retire on Azure), map:
#   DEPLOYMENT_MAIN → gpt-5         (or gpt-5-mini for mid-tier)
#   DEPLOYMENT_FAST → gpt-5-mini    (or gpt-5-nano for lowest cost)
# The routing logic below requires no changes — only env vars change.
# At GPT-5 pricing (gpt-5: ~$2.50/1M, gpt-5-nano: ~$0.05/1M), smart
# routing will save Royal London significant cost at scale vs routing
# everything to the full model.
TECHNICAL_COMPLEX_KEYWORDS = [
    # Annual allowance complexity
    "tapered annual", "tapered allowance",
    "money purchase annual allowance", "mpaa",
    "carry forward",
    # Lump sum rules
    "tax-free lump sum", "tax free lump sum",
    "25% tax", "lump sum and continue",
    "trivial commutation", "small pots rule",
    # DB pension specific
    "defined benefit", "final salary",
    "transfer value", "pension transfer value",
    "section 32",
    # Legacy protection regimes
    "lifetime allowance", "lta",
    "enhanced protection", "fixed protection",
    # Complex product mechanics
    "pension recycling",
    "guaranteed annuity rate",
    "with-profits", "with profits",
    "unit linked",
]


def is_simple_query(
    query: str,
    is_sensitive: bool,
    query_type: str = "SPECIFIC",
) -> bool:
    """
    Route to gpt-4o/DEPLOYMENT_FAST (True) or gpt-4.1/DEPLOYMENT_MAIN (False).

    v2.0.0 — rewritten with signal-based logic and targeted technical
    keyword list. Validated against 55 queries (25 original + 30 edge
    cases), all routing correctly.

    ROUTING PRIORITY (checked in order — first match wins):

    1. is_sensitive → MAIN (gpt-4.1)
       Empathy/bereavement queries need tone precision and FCA
       disclaimer consistency that gpt-4.1 delivers more reliably.

    2. TECHNICAL_COMPLEX_KEYWORDS → MAIN
       Narrow list of HMRC/FCA regulated concepts that are genuinely
       non-obvious from structural signals (word count, "compare" etc).
       Fixes CPX-02 (tapered annual allowance) and CPX-05 (tax-free
       lump sum) which were previously routing to gpt-4o-mini because
       they are short queries with no structural complexity signals.

    3. Structural complexity → MAIN
       compare / difference between / versus / calculate / >20 words /
       multiple question marks — signals a multi-concept query that
       needs the stronger model regardless of topic.

    4. BROAD query type + no complexity → FAST (gpt-4o)
       Overview/entry-point queries ("what types of pensions...",
       "explain drawdown", "tell me about...") — gpt-4o handles these
       well. Previously these were being over-escalated to gpt-4.1 by
       the Sprint 1 BROAD→False enhancement, which was wrong.

    5. SPECIFIC with ≥2 simple indicators → FAST (gpt-4o)
       Short, simple, single-concept queries with a question mark and
       a simple query word pattern.

    6. Default → MAIN
       When in doubt, use the stronger model. Currently gpt-4.1 is
       actually 20% CHEAPER than gpt-4o on Azure (£2.00 vs £2.50/1M
       input tokens), so the default is both safer and cheaper.

    MIGRATION NOTE:
    query_type parameter (from state.query_type set by classifier_node)
    is used in step 4. It is also used in retriever.py for the
    title_questions scoring profile — the two uses are independent.

    Validated routing for 55 queries: 55/55 correct.
    """
    # 1. Sensitive always → gpt-4.1
    if is_sensitive:
        return False

    query_lower = query.lower()

    # 2. Technical pension/tax complexity → gpt-4.1
    if any(kw in query_lower for kw in TECHNICAL_COMPLEX_KEYWORDS):
        return False

    # 3. Structural complexity → gpt-4.1
    complex_indicators = [
        "compare" in query_lower,
        "difference between" in query_lower,
        "versus" in query_lower or " vs " in query_lower,
        "calculate" in query_lower,
        len(query.split()) > 20,
        query.count("?") > 1,
    ]
    if any(complex_indicators):
        return False

    # 4. BROAD query, no complexity detected → gpt-4o
    if query_type == "BROAD":
        return True

    # 5. SPECIFIC query — use indicator heuristic
    simple_indicators = [
        len(query.split()) < 10,
        "?" in query and query.count("?") == 1,
        any(w in query_lower for w in [
            "what is", "how do i", "can i",
            "where", "when", "who", "contact",
            "phone", "number", "what happens",
            # NOTE: "should i" intentionally excluded — it signals a
            # financial decision query (needs_disclaimer=True in
            # supervisor.py) and must route to DEPLOYMENT_MAIN.
        ]),
    ]
    return sum(simple_indicators) >= 2


def clean_response_text(text: str) -> str:
    """
    Post-process LLM response to fix formatting issues:
    1. Strips markdown horizontal rules (--- / *** / ___)
       that gpt-4.1 occasionally inserts between sections.
    2. Removes single-asterisk wrapping from disclaimer lines
       (*disclaimer text* → disclaimer text).
    3. Strips trailing whitespace per line.
    4. Collapses 3+ consecutive blank lines → max 2.
    """
    lines   = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()

        # Drop markdown horizontal rules
        if re.match(r"^[-*_]{3,}$", stripped):
            continue

        # Strip single-asterisk wrapping (not **bold** or bullet point)
        if (
            stripped.startswith("*")
            and stripped.endswith("*")
            and not stripped.startswith("**")
            and not stripped.startswith("* ")
        ):
            line = line.replace(stripped, stripped[1:-1])

        cleaned.append(line.rstrip())

    # Collapse 3+ blank lines to max 2
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned))
    return result.strip()


def make_static_citations(
    *dicts: dict,
    existing: list["Citation"] | None = None,
) -> list["Citation"]:
    """
    Build a list of Citation objects from static dicts
    (CONTACT_CITATION, ADVISER_CITATION, BEREAVEMENT_CITATION)
    appended after any existing retrieved-source citations.

    Deduplicates by URL. Numbering is sequential starting
    after the last existing citation index (or from 1 if none).
    Called whenever a response redirects the customer to a
    Royal London page that has no [n] marker in the LLM text.
    """
    base    = existing or []
    seen    = {c.url for c in base}
    counter = (max(c.index for c in base) + 1) if base else 1
    result  = list(base)
    for d in dicts:
        if d["url"] not in seen:
            result.append(Citation(
                index=counter,
                url=d["url"],
                title=d["title"],
                section=d["section"],
            ))
            seen.add(d["url"])
            counter += 1
    return result


def extract_citations(
    state: AgentState,
    response_text: str,
) -> tuple[str, list[Citation]]:
    """
    Extract citations and renumber sequentially
    by order of first appearance in text.
    Returns updated response text + citations list.

    BUG #12 FIX (July 2026, Mukesh Kund): the previous version
    renumbered every marker in the text FIRST, then built the
    citations list, dropping entries whose source_url or chunk
    index was already seen. That left the text with a rendered
    [2] (etc.) with no matching citation pill whenever two cited
    indices shared a source_url, or the model hallucinated a
    citation number beyond len(retrieved_chunks). Fixed by
    resolving/filtering candidates FIRST, assigning final
    sequential numbers only to survivors, and removing orphan
    markers from the text entirely instead of leaving a dangling
    number.
    """
    all_markers = re.findall(r'\[(\d+)\]', response_text)

    if not all_markers:
        return response_text, []

    # First-appearance order of original marker numbers.
    first_seen_order = []
    for num in all_markers:
        if num not in first_seen_order:
            first_seen_order.append(num)

    # Resolve each original number to a chunk, filtering out
    # anything invalid (out of range) or a duplicate (same chunk
    # index or same source_url already used) — BEFORE assigning
    # final numbers, so only survivors ever get a number in the
    # rendered text.
    citations     = []
    orig_to_final = {}
    seen_indices  = set()
    seen_urls     = set()
    next_num      = 1

    for orig_num in first_seen_order:
        idx = int(orig_num) - 1
        if idx in seen_indices:
            continue  # same chunk cited under a second number
        if not (0 <= idx < len(state.retrieved_chunks)):
            continue  # hallucinated citation number — drop marker
        chunk = state.retrieved_chunks[idx]
        if chunk.source_url in seen_urls:
            continue  # different chunk, same page already cited
        seen_indices.add(idx)
        seen_urls.add(chunk.source_url)
        orig_to_final[orig_num] = next_num
        citations.append(Citation(
            index=next_num,
            url=chunk.source_url,
            section=chunk.section,
            title=chunk.title,
        ))
        next_num += 1

    # Renumber in text — valid markers get their final number,
    # orphan markers (no surviving citation) are removed entirely
    # rather than left as a dangling [n].
    def replace_citation(match):
        orig = match.group(1)
        if orig in orig_to_final:
            return f"[{orig_to_final[orig]}]"
        return ""

    updated_text = re.sub(
        r'\[(\d+)\]', replace_citation, response_text
    )
    # Collapse any double space left behind by a removed marker.
    updated_text = re.sub(r' {2,}', ' ', updated_text)

    return updated_text, citations


# ── Main node ─────────────────────────────────────────────────
def _finalize_response(
    state: AgentState,
    deployment: str,
    raw_response: str,
    usage_data,
    start: float,
    recovered_from_history_contamination: bool = False,
) -> None:
    """
    Shared post-processing for a raw LLM response: clean text,
    extract/renumber citations, UNKNOWN PRODUCT RULE detection,
    static citation injection, token tracking, and final logging.

    v2.6.0 — extracted from generator_node()'s main success path
    so the content-filter retry-without-history path (also new in
    v2.6.0) can produce an identically-processed response instead
    of a hand-duplicated, easy-to-drift copy of this logic.

    Mutates state in place. Does not set state.stream_tokens —
    callers set that themselves since the shape differs between
    the streaming path (list of chunks) and the non-streaming
    retry path (single-element list).
    """
    state.model_used = deployment

    # Clean formatting issues
    raw_response = clean_response_text(raw_response)

    # Extract + renumber citations
    updated_text, citations = extract_citations(state, raw_response)

    # ── UNKNOWN PRODUCT RULE refusal detection (v1.6.0) ──
    if UNKNOWN_PRODUCT_RESPONSE in updated_text:
        state.refusal_triggered = True
        state.raw_response      = UNKNOWN_PRODUCT_RESPONSE
        state.final_response    = UNKNOWN_PRODUCT_RESPONSE
        state.citations = make_static_citations(CONTACT_CITATION)
    # ── ACCOUNT ACCESS RULE detection (BUG #27 FIX) ──
    elif ACCOUNT_ACCESS_RESPONSE in updated_text:
        state.refusal_triggered = True
        state.raw_response      = ACCOUNT_ACCESS_RESPONSE
        state.final_response    = ACCOUNT_ACCESS_RESPONSE
        state.citations = make_static_citations(CONTACT_CITATION)
    else:
        state.raw_response = updated_text
        # ── Static citation injection (v2.2.0) ────────────
        static_extras: list[dict] = []

        # BUG #28 FIX: detect the disclaimer TEXT directly, not just
        # the needs_disclaimer flag. The LLM decides independently
        # (FINANCIAL DISCLAIMER RULE) whether to write this
        # paragraph — it can (and does) write it on queries the
        # FINANCIAL_DECISION_TRIGGERS keyword list didn't predict.
        # Without this, the disclaimer's own "please see the link
        # in our resources below" has no link behind it.
        has_disclaimer_text = FINANCIAL_DISCLAIMER_TEXT in updated_text

        if state.__dict__.get("_bereavement"):
            static_extras = [BEREAVEMENT_CITATION, CONTACT_CITATION]
        elif state.needs_disclaimer or has_disclaimer_text:
            static_extras = [ADVISER_CITATION]
        elif state.needs_empathy:
            static_extras = [ADVISER_CITATION, CONTACT_CITATION]

        if static_extras:
            state.citations = make_static_citations(
                *static_extras, existing=citations
            )
        else:
            state.citations = citations

    # Track token usage (v2.3.0)
    if usage_data:
        cached = 0
        try:
            cached = usage_data.prompt_tokens_details.cached_tokens or 0
        except AttributeError:
            pass

        state.token_usage = {
            "input_tokens":  usage_data.prompt_tokens,
            "output_tokens": usage_data.completion_tokens,
            "total_tokens":  usage_data.total_tokens,
            "cached_tokens": cached,
        }
        track_token_usage(
            model=deployment,
            input_tokens=usage_data.prompt_tokens,
            output_tokens=usage_data.completion_tokens,
        )
        if cached > 0:
            log.info(
                "kv_cache_hit",
                cached_tokens=cached,
                model=deployment,
                request_id=state.request_id,
            )
        else:
            log.debug(
                "kv_cache_miss",
                model=deployment,
                request_id=state.request_id,
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
        recovered_from_history_contamination=recovered_from_history_contamination,
    )


def generator_node(state: AgentState) -> AgentState:
    """
    Generate response using gpt-4.1 (DEPLOYMENT_MAIN) or
    gpt-4o (DEPLOYMENT_FAST). Reads state.built_prompt set
    by prompt_builder_node — pure LLM-call node, no prompt
    construction logic in this file.

    Model routing (v2.0.0 — enhanced with query_type):
    - BROAD queries or sensitive (empathy) → gpt-4.1
    - Simple SPECIFIC queries → gpt-4o
    - Complex SPECIFIC queries → gpt-4.1

    See is_simple_query() for full routing logic.
    """
    start = time.time()

    # state.needs_empathy, state.needs_disclaimer,
    # state.is_sensitive — set by supervisor.py (v1.5.0).
    # state.built_prompt — set by prompt_builder_node (v2.0.0).
    # state.query_type — set by classifier_node (v2.0.0).

    try:
        # Route: gpt-4o (fast) vs gpt-4.1 (main)
        deployment = (
            DEPLOYMENT_FAST
            if is_simple_query(
                state.query,
                state.is_sensitive,
                state.query_type,
            )
            else DEPLOYMENT_MAIN
        )

        client = get_openai_client()

        # Read the pre-assembled prompt from prompt_builder_node.
        # Defensive fallback: if built_prompt is somehow empty
        # (shouldn't happen in normal flow but guarded for
        # resilience), use a minimal safe prompt.
        user_prompt = state.built_prompt
        if not user_prompt:
            log.warning(
                "built_prompt_empty",
                request_id=state.request_id,
                note="prompt_builder_node may not have run",
            )
            user_prompt = (
                f"Customer question: {state.query}\n\n"
                f"Answer using information about Royal London's "
                f"insurance, pensions and ISA products only."
            )

        # BROAD queries (overview, multi-product) need more tokens.
        # SPECIFIC queries: 800 is sufficient for a concise FAQ answer.
        # GPT-5 reasoning models (gpt-5*, not gpt-4*): max_completion_tokens
        # covers BOTH internal reasoning tokens AND output tokens combined.
        # If reasoning consumes the full budget, output is empty string.
        # Fix: raise ceiling significantly for GPT-5 so reasoning has
        # headroom without starving the actual response content.
        is_gpt5 = "gpt-4" not in deployment.lower()
        if is_gpt5:
            max_tokens = 4000 if state.query_type == "BROAD" else 3000
        else:
            max_tokens = 1200 if state.query_type == "BROAD" else 800

        # ── True token streaming (v2.3.0) ────────────────────
        # stream=True returns an iterator of chunks immediately.
        # We collect tokens into state.stream_tokens so server.py
        # can yield them directly to the client without waiting
        # for the full response — perceived latency drops to
        # time-to-first-token (~500ms) instead of total gen time.
        # generator_node remains synchronous (no async refactor
        # needed) — the stream iterator is consumed here inside
        # run_in_executor.
        stream = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            **_build_create_kwargs(deployment, max_tokens, 0.1),
            stream=True,
            stream_options={"include_usage": True},
        )

        tokens: list[str] = []
        usage_data = None

        for chunk in stream:
            # Final chunk carries usage when include_usage=True
            if chunk.usage:
                usage_data = chunk.usage

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                tokens.append(delta.content)

        raw_response = "".join(tokens)

        # ── Non-streaming fallback (v2.5.0) ──────────────────
        # Known Azure OpenAI issue: gpt-5-mini and gpt-5.6-luna
        # return empty choices [] when stream=True via Chat
        # Completions API. Confirmed via Microsoft Tech Community.
        # Fix: retry as stream=False when tokens is empty.
        if not raw_response.strip():
            log.warning(
                "stream_empty_fallback",
                deployment=deployment,
                note="stream=True returned empty — retrying non-streaming",
            )
            fallback = client.chat.completions.create(
                model=deployment,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                **_build_create_kwargs(deployment, max_tokens, 0.1),
                stream=False,
            )
            raw_response      = fallback.choices[0].message.content or ""
            usage_data        = fallback.usage
            state.stream_tokens = [raw_response]  # single chunk
        else:
            state.stream_tokens = tokens   # server.py reads this

        # ── Shared post-processing (v2.6.0) ───────────────────
        # Extracted to _finalize_response() so this exact logic
        # is also used by the content-filter retry-without-history
        # path in the except block below — no hand-duplicated,
        # drift-prone copy.
        _finalize_response(state, deployment, raw_response, usage_data, start)

    except Exception as e:
        error_str = str(e)
        is_filter, filter_category = is_content_filter_error(error_str)

        if is_filter:
            log.warning(
                "generator_content_filter_triggered",
                category=filter_category,
                has_history=bool(state.conversation_history),
                request_id=state.request_id,
            )

            # Retry ONCE with conversation history stripped from
            # the prompt. Distinguishes a genuine block on THIS
            # query (Case A — fall through to refusal below) from
            # toxic history from an earlier turn poisoning an
            # otherwise innocent current query (Case B — recover
            # and serve the retried response normally). See v2.6.0
            # changelog for full rationale and a worked example.
            if state.conversation_history and state.built_prompt:
                stripped_prompt = strip_history_from_prompt(state.built_prompt)
                if stripped_prompt != state.built_prompt:
                    try:
                        retry_deployment = (
                            DEPLOYMENT_FAST
                            if is_simple_query(
                                state.query,
                                state.is_sensitive,
                                state.query_type,
                            )
                            else DEPLOYMENT_MAIN
                        )
                        retry_client  = get_openai_client()
                        retry_is_gpt5 = "gpt-4" not in retry_deployment.lower()
                        retry_max_tokens = (
                            (4000 if state.query_type == "BROAD" else 3000)
                            if retry_is_gpt5
                            else (1200 if state.query_type == "BROAD" else 800)
                        )
                        retry_response = retry_client.chat.completions.create(
                            model=retry_deployment,
                            messages=[
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user",   "content": stripped_prompt},
                            ],
                            **_build_create_kwargs(
                                retry_deployment, retry_max_tokens, 0.1
                            ),
                            stream=False,
                        )
                        retry_text = (
                            retry_response.choices[0].message.content or ""
                        )

                        if retry_text.strip():
                            log.info(
                                "history_contamination_recovered",
                                category=filter_category,
                                request_id=state.request_id,
                            )
                            state.stream_tokens = [retry_text]
                            _finalize_response(
                                state,
                                retry_deployment,
                                retry_text,
                                retry_response.usage,
                                start,
                                recovered_from_history_contamination=True,
                            )
                            return state

                    except Exception as retry_e:
                        log.warning(
                            "history_contamination_retry_failed",
                            error=str(retry_e),
                            request_id=state.request_id,
                        )
                        # fall through to the refusal below

            # Either: no history to strip, retry also failed, or
            # this is a genuine block on the current query itself
            # (Case A). RefusalReason.HARMFUL reads as an honest
            # boundary rather than a system error.
            log.error(
                "generator_error",
                error=error_str,
                content_filter=True,
                category=filter_category,
                request_id=state.request_id,
            )
            from core.refusal import get_refusal, RefusalReason
            state.refusal_triggered = True
            state.final_response    = get_refusal(RefusalReason.HARMFUL)
            state.citations = make_static_citations(CONTACT_CITATION)
            return state

        # Non-content-filter exceptions — unchanged from v2.5.0
        log.error(
            "generator_error",
            error=error_str,
            request_id=state.request_id,
        )
        from core.refusal import get_refusal, RefusalReason
        state.refusal_triggered = True
        state.final_response    = get_refusal(
            RefusalReason.GENERAL
        )
        state.citations = make_static_citations(CONTACT_CITATION)

    return state