"""
Formal refusal templates for different query categories.
"""

from enum import Enum


class RefusalReason(Enum):
    HARMFUL = "harmful"
    IRRELEVANT = "irrelevant"
    NO_RESULTS = "no_results"
    GENERAL = "general"
    OUT_OF_SCOPE = "out_of_scope"
    UNSAFE_OUTPUT = "unsafe_output"
    PII_DETECTED = "pii"


REFUSAL_TEMPLATES = {
    RefusalReason.HARMFUL: (
        "I'm unable to assist with that request. "
        "If you have a genuine Royal London insurance "
        "or pension query, I'm happy to help."
    ),

    RefusalReason.IRRELEVANT: (
        "I can only help with questions about Royal London "
        "insurance, pensions, ISAs and related financial "
        "products. Is there anything in those areas "
        "I can help you with today?"
    ),

    RefusalReason.OUT_OF_SCOPE: (
        "I can only help with questions about Royal London "
        "insurance, pensions, ISAs and related financial "
        "products. Is there anything in those areas "
        "I can help you with today?"
    ),

    RefusalReason.NO_RESULTS: (
        "I wasn't able to find specific information about "
        "that in our knowledge base. For detailed assistance "
        "please see our contact page below."
    ),

    RefusalReason.GENERAL: (
        "I'm unable to process your request at the moment. "
        "Please see our contact page below."
    ),

    RefusalReason.UNSAFE_OUTPUT: (
        "I'm unable to process your request at the moment. "
        "Please see our contact page below."
    ),

    RefusalReason.PII_DETECTED: (
        "For your security, I can't process messages that "
        "contain personal or account details. Please rephrase "
        "your question without this information, or see our "
        "contact page below for account-specific help."
    ),
}


def get_refusal(reason: RefusalReason) -> str:
    """Get refusal message for given reason."""
    return REFUSAL_TEMPLATES.get(reason, REFUSAL_TEMPLATES[RefusalReason.GENERAL])