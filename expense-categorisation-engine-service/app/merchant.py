import re
import unicodedata


_WHITESPACE = re.compile(r"\s+")


def normalize_merchant_name(merchant_name: str) -> str:
    """Create the stable key used for cache lookups and feedback rules.

    This intentionally does not remove bank-specific prefixes or merchant
    suffixes. Those transformations need transaction-source-specific rules.
    """
    normalized = unicodedata.normalize("NFKC", merchant_name)
    normalized = _WHITESPACE.sub(" ", normalized.strip())
    return normalized.upper()
