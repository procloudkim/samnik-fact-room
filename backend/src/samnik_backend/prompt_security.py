from __future__ import annotations

import re
from dataclasses import dataclass


_UNTRUSTED_INSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+instructions?\b",
        r"\b(?:reveal|show|print|dump|expose)\b.{0,80}\b(?:system|developer)\s+(?:prompt|message)\b",
        r"\b(?:reveal|show|print|dump|expose)\b.{0,80}\b(?:api[_ -]?key|database[_ -]?url|secret|\.env)\b",
        r"\b(?:execute|run)\b.{0,60}\b(?:shell|command|tool\s*call|raw\s*sql)\b",
        r"(?:이전|위의|모든)\s*(?:지시|명령).{0,20}(?:무시|폐기)",
        r"(?:시스템\s*프롬프트|개발자\s*메시지).{0,40}(?:보여|출력|공개|노출)",
        r"(?:API\s*키|데이터베이스\s*URL|환경\s*변수|비밀값).{0,40}(?:보여|출력|공개|노출)",
        r"(?:쉘|셸|명령어|도구\s*호출|원시\s*SQL).{0,30}(?:실행|호출)",
    )
)


@dataclass(frozen=True, slots=True)
class PromptSecurityAssessment:
    blocked: bool
    reason_code: str | None = None


def assess_user_message(message: str) -> PromptSecurityAssessment:
    """Detect instruction override and secret exfiltration without logging it."""

    if contains_untrusted_instruction(message):
        return PromptSecurityAssessment(
            blocked=True,
            reason_code="UNTRUSTED_INSTRUCTION_REQUEST",
        )
    return PromptSecurityAssessment(blocked=False)


def contains_untrusted_instruction(text: str | None) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in _UNTRUSTED_INSTRUCTION_PATTERNS)
