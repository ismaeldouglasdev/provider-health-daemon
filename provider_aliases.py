"""Provider alias maps and canonical prefix normalization.

Leaf module (no internal imports) so any daemon component can normalize
provider names without creating import cycles.

Connection names come from the 9router /api/providers admin API
(e.g. "kiro", "bazaarlink"); the 9router catalog addresses them by short
canonical prefixes (e.g. "kr", "bzl"). normalize_provider maps either form
to the canonical prefix.
"""

from typing import Dict

# ── Bidirectional provider alias map ──────────────────────────────────────
# Connection names (from 9router /api/providers) ↔ catalog prefixes
PROVIDER_ALIAS_MAP: Dict[str, str] = {
    # Direct mappings: connection name → catalog prefix
    "kiro": "kr",
    "cursor": "cu",
    "bazaarlink": "bzl",
    "sambanova": "samba",
    "byteplus": "bpm",
    "grok-cli": "gcli",
    "cloudflare-ai": "cf",
    "cf-ai": "cf",
    "antigravity": "ag",
    "codex": "cx",
    "github": "gh",
    "poolside": "ps",
    "api-airforce": "af",
    "kilocode": "kc",
    "cline": "cl",

    # OpenAI-compatible chat UUID connections → short prefixes
    "openai-compatible-chat-50188b71-3f95-463f-9d88-45b655f507cd": "rw",
    "openai-compatible-chat-09c4fe6c-3979-456f-b5fc-f9f589c7e721": "any",
    "openai-compatible-chat-e27ce9fc-dfed-45ee-a6c8-fe8519f2e636": "hf",
}

# Build reverse map for prefix → connection name lookups
PREFIX_TO_ALIAS: Dict[str, str] = {v: k for k, v in PROVIDER_ALIAS_MAP.items()}


def normalize_provider(name: str) -> str:
    """Return canonical catalog prefix for any connection name or prefix.

    Args:
        name: Connection name (e.g., "kiro", "kr") or catalog prefix (e.g., "kr", "kiro").

    Returns:
        Canonical catalog prefix (lowercase). Unknown names are lowercased and
        passed through unchanged.

    Examples:
        normalize_provider("kiro") → "kr"
        normalize_provider("kr") → "kr"
        normalize_provider("unknown") → "unknown"
        normalize_provider("OpenAI-Compatible-Chat") → "rw"
        normalize_provider("rw") → "rw"

    Note:
        Idempotent: normalize_provider(normalize_provider(x)) == normalize_provider(x).
    """
    if not name:
        return ""

    normalized = name.strip().lower()

    # Check alias map (connection name → prefix)
    if normalized in PROVIDER_ALIAS_MAP:
        return PROVIDER_ALIAS_MAP[normalized]

    # Check reverse map (prefix → connection name)
    if normalized in PREFIX_TO_ALIAS:
        return normalized

    # Unknown: pass through normalized
    return normalized
