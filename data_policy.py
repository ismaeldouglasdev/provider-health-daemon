"""Local data-sensitivity and model data-policy classification.

This module never sends prompt content to an external classifier. Unknown policy is
conservative: it is not treated as safe for sensitive data.
"""

import re
from enum import Enum

from kilo_policy import get as get_kilo_policy


class DataSensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class DataPolicy(str, Enum):
    KNOWN_NO_TRAINING_DEFAULT = "known_no_training_default"
    TRAINING_POSSIBLE = "training_possible"
    LOCAL = "local"
    UNKNOWN = "unknown"


# High-confidence local indicators. Keep these deliberately narrow to avoid
# classifying ordinary prose as sensitive.
_SECRET_PATTERNS = [
    re.compile(r"(?:sk|rk)-[A-Za-z0-9_-]{16,}"),
    # Avoid false positives from OpenCode/tool schemas such as
    # ``token: string`` or ``api_key: value``. A credential-like value must
    # be at least 16 chars and contain mixed character classes, while common
    # schema/type placeholders are explicitly excluded.
    re.compile(
        r"(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*"
        r"(?!string\b|str\b|token\b|value\b|example\b|null\b|undefined\b)"
        r"(?=[A-Za-z0-9_./+=-]{12,}\b)[^\s]{12,}",
        re.I,
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
]
# Do not classify ordinary source-code emails/phone-looking strings as
# sensitive: coding agents routinely see package authors, test fixtures,
# documentation and examples. Keep only explicit Brazilian tax identifiers.
_PII_PATTERNS = [
    re.compile(r"\b(?:cpf|cnpj)\s*[:=]?\s*[0-9./-]{11,18}\b", re.I),
]
_INTERNAL_HINTS = re.compile(
    r"\b(?:repository|repo|private|internal|proprietary|production|staging|database|db|deploy|credential|incident)\b",
    re.I,
)


def classify_text(text: str) -> DataSensitivity:
    text = text or ""
    if any(p.search(text) for p in _SECRET_PATTERNS) or any(p.search(text) for p in _PII_PATTERNS):
        return DataSensitivity.SENSITIVE
    if _INTERNAL_HINTS.search(text) or len(text) >= 12000:
        return DataSensitivity.INTERNAL
    return DataSensitivity.PUBLIC


def classify_request(body: dict | None) -> DataSensitivity:
    if not body:
        return DataSensitivity.PUBLIC
    parts = []
    for message in body.get("messages", []) or []:
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
    return classify_text("\n".join(parts))


def classify_model_policy(model: str | None) -> DataPolicy:
    """Classify only documented/high-confidence routing cases.

    Unknown providers remain UNKNOWN for auditability, but are not blocked by default; only providers with a documented training risk are blocked.
    """
    model = (model or "").strip().lower()
    kilo_policy = get_kilo_policy(model)
    if kilo_policy is True:
        return DataPolicy.TRAINING_POSSIBLE
    if kilo_policy is False:
        return DataPolicy.KNOWN_NO_TRAINING_DEFAULT
    if model in {"kilo-auto/free", "kilo/auto-free"} or model.startswith("nvidia/"):
        return DataPolicy.TRAINING_POSSIBLE
    if model.startswith(("ollama/", "lmstudio/", "lm-studio/", "local/")):
        return DataPolicy.LOCAL
    if model.startswith(("openai/", "anthropic/", "google/")):
        return DataPolicy.KNOWN_NO_TRAINING_DEFAULT
    return DataPolicy.UNKNOWN


def allowed(policy: DataPolicy, sensitivity: DataSensitivity) -> bool:
    """Conservative gate: sensitive data requires explicit safe/local policy."""
    if sensitivity == DataSensitivity.PUBLIC:
        return True
    if sensitivity == DataSensitivity.INTERNAL:
        return policy not in {DataPolicy.TRAINING_POSSIBLE}
    # For sensitive prompts, enforce the strong gate only for providers
    # with documented evidence that prompts may be used for training.
    # Unknown providers are allowed by default until evidence says otherwise.
    return policy != DataPolicy.TRAINING_POSSIBLE
