"""Deterministic relevance and speaker gate for ambient transcript segments.

This module is intentionally side-effect free.  It does not call Hermes, store raw
transcripts, or access external services.  Callers may archive the original content
separately, but only an owner-attributed or explicitly dispatchable ambient result can
produce a Hermes envelope.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping


class Provenance(str, Enum):
    UNKNOWN = "unknown"
    OMI = "omi"
    NATIVE_AUTHENTICATED = "native_authenticated"


class OwnerStatus(str, Enum):
    OWNER = "owner"
    NON_OWNER = "non_owner"
    UNKNOWN = "unknown"


class Disposition(str, Enum):
    ACTION = "action"
    IGNORE = "ignore"
    HOLD = "hold"


class Category(str, Enum):
    REPOSITORY = "repository"
    WEB_TASK = "web_task"
    TOOL = "tool"
    STARTUP = "startup"
    PERFORMANCE = "performance"
    TODO = "todo"
    EVENT = "event"
    CONTACT = "contact"
    GOAL = "goal"
    ORDINARY_LIFE = "ordinary_life"


CATEGORY_ORDER: tuple[Category, ...] = (
    Category.REPOSITORY,
    Category.WEB_TASK,
    Category.TOOL,
    Category.STARTUP,
    Category.PERFORMANCE,
    Category.TODO,
    Category.EVENT,
    Category.CONTACT,
    Category.GOAL,
)


@dataclass(frozen=True)
class SpeakerContext:
    """Claims supplied by a capture adapter.

    ``is_user`` is trusted only for Omi provenance and only when it is an actual
    boolean.  Native owner status is trusted only when the adapter has already
    attributed the speaker and sets ``authenticated_owner`` explicitly. The field
    name is retained for stored-record compatibility. ``ambient_authorized`` is a
    separate capture-channel dispatch claim honored only for Omi provenance.
    """

    provenance: Provenance = Provenance.UNKNOWN
    is_user: bool | None = None
    authenticated_owner: bool | None = None
    principal_fingerprint: str | None = None
    ambient_authorized: bool = False

    @classmethod
    def unknown(cls) -> "SpeakerContext":
        return cls()

    @classmethod
    def omi(
        cls, *, is_user: bool | None, ambient_authorized: bool = False
    ) -> "SpeakerContext":
        return cls(
            provenance=Provenance.OMI,
            is_user=is_user,
            ambient_authorized=ambient_authorized,
        )

    @classmethod
    def native_owner(cls, principal_fingerprint: str) -> "SpeakerContext":
        return cls(
            provenance=Provenance.NATIVE_AUTHENTICATED,
            authenticated_owner=True,
            principal_fingerprint=principal_fingerprint,
        )

    @classmethod
    def native_non_owner(cls, principal_fingerprint: str) -> "SpeakerContext":
        return cls(
            provenance=Provenance.NATIVE_AUTHENTICATED,
            authenticated_owner=False,
            principal_fingerprint=principal_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance.value,
            "is_user": self.is_user,
            "authenticated_owner": self.authenticated_owner,
            "principal_fingerprint": self.principal_fingerprint,
            "ambient_authorized": self.ambient_authorized,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SpeakerContext":
        return cls(
            provenance=Provenance(str(value.get("provenance", "unknown"))),
            is_user=value.get("is_user") if isinstance(value.get("is_user"), bool) else None,
            authenticated_owner=(
                value.get("authenticated_owner")
                if isinstance(value.get("authenticated_owner"), bool)
                else None
            ),
            principal_fingerprint=(
                str(value["principal_fingerprint"])
                if value.get("principal_fingerprint") is not None
                else None
            ),
            ambient_authorized=value.get("ambient_authorized") is True,
        )


@dataclass(frozen=True)
class Evidence:
    rule_id: str
    category: Category | None
    confidence: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value if self.category else None,
            "confidence": self.confidence,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class CorrectionRecord:
    """A content-addressed user correction; it deliberately contains no raw text."""

    content_fingerprint: str
    disposition: Disposition
    categories: tuple[Category, ...] = ()
    reason_code: str = "user_correction"

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_fingerprint": self.content_fingerprint,
            "disposition": self.disposition.value,
            "categories": [category.value for category in self.categories],
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CorrectionRecord":
        return cls(
            content_fingerprint=str(value["content_fingerprint"]),
            disposition=Disposition(str(value["disposition"])),
            categories=tuple(Category(str(item)) for item in value.get("categories", [])),
            reason_code=str(value.get("reason_code", "user_correction")),
        )


@dataclass(frozen=True)
class InterestWeight:
    category: Category
    weight: float
    source: str = "explicit"

    def __post_init__(self) -> None:
        if not -1.0 <= self.weight <= 1.0:
            raise ValueError("interest weight must be between -1.0 and 1.0")

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category.value, "weight": self.weight, "source": self.source}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterestWeight":
        return cls(
            category=Category(str(value["category"])),
            weight=float(value["weight"]),
            source=str(value.get("source", "explicit")),
        )


@dataclass(frozen=True)
class InterestModel:
    """JSON-safe, immutable interest and correction state."""

    version: int = 1
    weights: tuple[InterestWeight, ...] = ()
    corrections: tuple[CorrectionRecord, ...] = ()

    def weight_for(self, category: Category) -> float:
        matching = [item.weight for item in self.weights if item.category == category]
        return matching[-1] if matching else 0.0

    def correction_for(self, fingerprint: str) -> CorrectionRecord | None:
        for correction in reversed(self.corrections):
            if correction.content_fingerprint == fingerprint:
                return correction
        return None

    def with_weight(self, weight: InterestWeight) -> "InterestModel":
        retained = tuple(item for item in self.weights if item.category != weight.category)
        return replace(self, version=self.version + 1, weights=retained + (weight,))

    def with_correction(self, correction: CorrectionRecord) -> "InterestModel":
        retained = tuple(
            item
            for item in self.corrections
            if item.content_fingerprint != correction.content_fingerprint
        )
        return replace(self, version=self.version + 1, corrections=retained + (correction,))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "weights": [item.to_dict() for item in self.weights],
            "corrections": [item.to_dict() for item in self.corrections],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterestModel":
        return cls(
            version=int(value.get("version", 1)),
            weights=tuple(InterestWeight.from_dict(item) for item in value.get("weights", [])),
            corrections=tuple(
                CorrectionRecord.from_dict(item) for item in value.get("corrections", [])
            ),
        )


@dataclass(frozen=True)
class RelevanceResult:
    """A decision record that never retains the input transcript."""

    content_fingerprint: str
    disposition: Disposition
    actionable: bool
    owner_status: OwnerStatus
    categories: tuple[Category, ...]
    primary_category: Category | None
    confidence: float
    reason_code: str
    evidence: tuple[Evidence, ...]
    ambient_authorized: bool = False

    @property
    def dispatch_allowed(self) -> bool:
        return self.actionable and (
            self.owner_status == OwnerStatus.OWNER or self.ambient_authorized
        )

    def hermes_payload(self, raw_text: str) -> dict[str, Any] | None:
        """Return raw content only for a verified actionable decision.

        HOLD and IGNORE results return ``None`` so uncertain content cannot be
        accidentally forwarded to Hermes by a normal caller.
        """

        if not self.dispatch_allowed:
            return None
        return {
            "content_fingerprint": self.content_fingerprint,
            "category": self.primary_category.value if self.primary_category else None,
            "categories": [category.value for category in self.categories],
            "text": raw_text,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_fingerprint": self.content_fingerprint,
            "disposition": self.disposition.value,
            "actionable": self.actionable,
            "dispatch_allowed": self.dispatch_allowed,
            "owner_status": self.owner_status.value,
            "ambient_authorized": self.ambient_authorized,
            "categories": [category.value for category in self.categories],
            "primary_category": self.primary_category.value if self.primary_category else None,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category: Category
    pattern: re.Pattern[str]
    confidence: float
    explanation: str


def _rx(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE)


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "repository.github_url",
        Category.REPOSITORY,
        _rx(r"\b(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+"),
        0.99,
        "Recognized a GitHub repository URL",
    ),
    _Rule(
        "repository.keyword",
        Category.REPOSITORY,
        _rx(r"\b(?:git(?:hub)?\s+)?repo(?:sitory)?\b|\bgithub\s+project\b|\bclone\s+it\b"),
        0.93,
        "Recognized repository intent",
    ),
    _Rule(
        "web_task.command",
        Category.WEB_TASK,
        _rx(
            r"\b(?:book|reserve|order|purchase|add\s+to\s+cart|"
            r"buy\s+(?:me\s+)?(?:a|an|the|some)\b|"
            r"check\b.{0,48}\b(?:price|cost)|(?:price|cost)\s+(?:of|for|watch|check)|"
            r"(?:what(?:'s| is)|how\s+much\s+is)\b.{0,48}\b(?:price|cost)|"
            r"fill\s+(?:out|in)|sign\s+(?:me\s+)?up\s+for|apply\s+(?:to|for)|"
            r"find\s+(?:me\s+)?(?:a|an|the|the\s+cheapest|cheap)\b|"
            r"search\s+(?:the\s+web|online)\s+for)\b"
        ),
        0.94,
        "Recognized a web task the computer-use agent can run",
    ),
    _Rule(
        "tool.software",
        Category.TOOL,
        _rx(
            r"\b(?:tool|framework|library|sdk|api|cli|package|plugin|extension|"
            r"mcp(?:\s+server)?|agent\s+platform)\b"
        ),
        0.88,
        "Recognized a software tool or integration",
    ),
    _Rule(
        "startup.company",
        Category.STARTUP,
        _rx(
            r"\b(?:startup|founder|cofounder|saas|venture|seed\s+round|series\s+[a-f]|"
            r"y\s*combinator|product\s+launch)\b"
        ),
        0.88,
        "Recognized a startup or company-building signal",
    ),
    _Rule(
        "performance.optimization",
        Category.PERFORMANCE,
        _rx(
            r"\b(?:latency|throughput|benchmark|profil(?:e|ing)|optimi[sz](?:e|ation)|"
            r"speed\s+up|run\s+faster|bottleneck|gpu\s+utili[sz]ation|memory\s+footprint)\b"
        ),
        0.92,
        "Recognized a performance or optimization signal",
    ),
    _Rule(
        "todo.explicit",
        Category.TODO,
        _rx(
            r"\b(?:to-?do|remind\s+me|action\s+item|add\s+(?:this|that|it)\s+"
            r"(?:to|as)\s+(?:a\s+)?(?:task|to-?do))\b"
        ),
        0.95,
        "Recognized an explicit task request",
    ),
    _Rule(
        "todo.commitment",
        Category.TODO,
        _rx(
            r"\b(?:i|we)\s+(?:need|have|must)\s+to\s+"
            r"(?:finish|build|research|check|send|deploy|update|fix|write|buy|book)\b"
        ),
        0.91,
        "Recognized a concrete work commitment",
    ),
    _Rule(
        "event.named",
        Category.EVENT,
        _rx(r"\b(?:hackathon|meetup|conference|workshop|webinar|rsvp|event\s+ticket)\b"),
        0.93,
        "Recognized an event signal",
    ),
    _Rule(
        "event.calendar",
        Category.EVENT,
        _rx(r"\b(?:add\s+to\s+(?:my\s+)?calendar|schedule\s+(?:a|the)?\s*meeting|register\s+for)\b"),
        0.94,
        "Recognized calendar or registration intent",
    ),
    _Rule(
        "contact.outreach",
        Category.CONTACT,
        _rx(
            r"\b(?:follow[ -]?up\s+with|reach\s+out\s+to|"
            r"send\s+(?:an?\s+)?(?:email|dm|message)\s+to|"
            r"(?:email|dm|message|contact|call)\s+"
            r"(?!(?:and|or|is|was|were|the|a|an|about|from|does|has|address)\b)"
            r"(?:the\s+)?[\w@])"
        ),
        0.93,
        "Recognized an outbound contact request",
    ),
    _Rule(
        "goal.explicit",
        Category.GOAL,
        _rx(r"\b(?:my|our)\s+(?:goal|objective|target|milestone)\b"),
        0.9,
        "Recognized an explicit goal",
    ),
    _Rule(
        "goal.intent",
        Category.GOAL,
        _rx(r"\b(?:i|we)\s+(?:want|plan)\s+to\s+(?:build|ship|launch|learn|create)\b"),
        0.87,
        "Recognized durable goal intent",
    ),
)


_ORDINARY_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "ordinary.food",
        _rx(
            r"\b(?:breakfast|lunch|dinner|pizza|sandwich|coffee|tea|hungry|delicious|"
            r"restaurant|groceries|dessert)\b"
        ),
        "Recognized ordinary food conversation",
    ),
    (
        "ordinary.travel",
        _rx(r"\b(?:weather|traffic|commute|parking|bus\s+was\s+late)\b"),
        "Recognized ordinary travel or weather conversation",
    ),
    (
        "ordinary.casual",
        _rx(r"\b(?:nice\s+day|good\s+morning|good\s+night|watch\s+a\s+movie|dog\s+is\s+cute)\b"),
        "Recognized casual conversation",
    ),
    (
        "ordinary.wellbeing",
        _rx(r"\b(?:i(?:'m|\s+am)\s+tired|take\s+a\s+nap|headache|go\s+to\s+sleep)\b"),
        "Recognized ordinary wellbeing conversation",
    ),
)


def content_fingerprint(text: str) -> str:
    clean = " ".join(text.split()).casefold()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def _speaker_assessment(context: SpeakerContext) -> tuple[OwnerStatus, float, Evidence]:
    if context.provenance == Provenance.OMI:
        if context.is_user is True:
            return (
                OwnerStatus.OWNER,
                0.96,
                Evidence("speaker.omi_explicit_owner", None, 0.96, "Omi explicitly marked is_user true"),
            )
        if context.is_user is False:
            return (
                OwnerStatus.NON_OWNER,
                0.99,
                Evidence("speaker.omi_explicit_other", None, 0.99, "Omi explicitly marked is_user false"),
            )
        return (
            OwnerStatus.UNKNOWN,
            0.98,
            Evidence("speaker.omi_unset", None, 0.98, "Omi did not provide an explicit owner claim"),
        )

    if context.provenance == Provenance.NATIVE_AUTHENTICATED:
        if context.authenticated_owner is True:
            return (
                OwnerStatus.OWNER,
                0.99,
                Evidence(
                    "speaker.native_app_owner",
                    None,
                    0.99,
                    "Native app attributed the speaker to the owner",
                ),
            )
        if context.authenticated_owner is False:
            return (
                OwnerStatus.NON_OWNER,
                0.99,
                Evidence(
                    "speaker.native_app_other",
                    None,
                    0.99,
                    "Native app attributed the speaker to a non-owner",
                ),
            )

    return (
        OwnerStatus.UNKNOWN,
        0.99,
        Evidence("speaker.unknown", None, 0.99, "No owner attribution provenance"),
    )


def _category_evidence(clean: str) -> tuple[tuple[Category, ...], tuple[Evidence, ...]]:
    matched: dict[Category, list[Evidence]] = {}
    for rule in _RULES:
        if rule.pattern.search(clean):
            matched.setdefault(rule.category, []).append(
                Evidence(rule.rule_id, rule.category, rule.confidence, rule.explanation)
            )
    categories = tuple(category for category in CATEGORY_ORDER if category in matched)
    evidence = tuple(item for category in categories for item in matched[category])
    return categories, evidence


def _ordinary_evidence(clean: str) -> Evidence | None:
    for rule_id, pattern, explanation in _ORDINARY_RULES:
        if pattern.search(clean):
            return Evidence(rule_id, Category.ORDINARY_LIFE, 0.98, explanation)
    return None


def _ordered_categories(categories: Iterable[Category]) -> tuple[Category, ...]:
    values = set(categories)
    return tuple(category for category in CATEGORY_ORDER if category in values)


def evaluate_relevance(
    text: str,
    speaker: SpeakerContext | None = None,
    interests: InterestModel | None = None,
) -> RelevanceResult:
    """Classify one transcript unit with an owner gate and deterministic rules."""

    speaker = speaker or SpeakerContext.unknown()
    interests = interests or InterestModel()
    clean = " ".join(text.split())
    fingerprint = content_fingerprint(clean)
    owner_status, owner_confidence, owner_evidence = _speaker_assessment(speaker)
    ambient_authorized = (
        speaker.provenance == Provenance.OMI and speaker.ambient_authorized is True
    )
    speaker_evidence = (owner_evidence,)
    if ambient_authorized:
        speaker_evidence += (
            Evidence(
                "speaker.omi_dispatchable_ambient",
                None,
                0.99,
                "The Omi capture channel was marked dispatchable",
            ),
        )
    categories, evidence = _category_evidence(clean)

    correction = interests.correction_for(fingerprint)
    if correction is not None:
        corrected_categories = _ordered_categories(correction.categories or categories)
        correction_evidence = Evidence(
            "correction.applied",
            corrected_categories[0] if corrected_categories else None,
            0.99,
            "Applied a content-addressed user correction",
        )
        all_evidence = speaker_evidence + (correction_evidence,)
        if (
            correction.disposition == Disposition.ACTION
            and owner_status != OwnerStatus.OWNER
            and not ambient_authorized
        ):
            return RelevanceResult(
                fingerprint,
                Disposition.HOLD,
                False,
                owner_status,
                corrected_categories,
                corrected_categories[0] if corrected_categories else None,
                owner_confidence,
                "owner_verification_required",
                all_evidence,
                ambient_authorized=ambient_authorized,
            )
        actionable = correction.disposition == Disposition.ACTION and bool(corrected_categories)
        return RelevanceResult(
            fingerprint,
            correction.disposition,
            actionable,
            owner_status,
            corrected_categories,
            corrected_categories[0] if corrected_categories else None,
            0.99,
            correction.reason_code,
            all_evidence,
            ambient_authorized=ambient_authorized,
        )

    if not clean:
        return RelevanceResult(
            fingerprint,
            Disposition.IGNORE,
            False,
            owner_status,
            (),
            None,
            1.0,
            "empty",
            speaker_evidence,
            ambient_authorized=ambient_authorized,
        )

    ordinary = _ordinary_evidence(clean)
    if not categories and ordinary is not None:
        return RelevanceResult(
            fingerprint,
            Disposition.IGNORE,
            False,
            owner_status,
            (Category.ORDINARY_LIFE,),
            Category.ORDINARY_LIFE,
            ordinary.confidence,
            "ordinary_life",
            speaker_evidence + (ordinary,),
            ambient_authorized=ambient_authorized,
        )

    if not categories:
        return RelevanceResult(
            fingerprint,
            Disposition.IGNORE,
            False,
            owner_status,
            (),
            None,
            0.86,
            "low_signal",
            speaker_evidence,
            ambient_authorized=ambient_authorized,
        )

    if owner_status != OwnerStatus.OWNER and not ambient_authorized:
        reason = "non_owner_speaker" if owner_status == OwnerStatus.NON_OWNER else "owner_unverified"
        return RelevanceResult(
            fingerprint,
            Disposition.HOLD,
            False,
            owner_status,
            categories,
            categories[0],
            owner_confidence,
            reason,
            speaker_evidence + evidence,
            ambient_authorized=ambient_authorized,
        )

    primary = categories[0]
    interest_weight = interests.weight_for(primary)
    if interest_weight <= -0.75:
        muted = Evidence(
            "interest.category_muted",
            primary,
            0.98,
            "The interest model explicitly muted this category",
        )
        return RelevanceResult(
            fingerprint,
            Disposition.IGNORE,
            False,
            owner_status,
            categories,
            primary,
            0.98,
            "interest_muted",
            speaker_evidence + evidence + (muted,),
            ambient_authorized=ambient_authorized,
        )

    signal_confidence = max(item.confidence for item in evidence)
    confidence = min(owner_confidence, max(0.5, signal_confidence + interest_weight * 0.05))
    return RelevanceResult(
        fingerprint,
        Disposition.ACTION,
        True,
        owner_status,
        categories,
        primary,
        round(confidence, 3),
        "owner_relevant_signal" if owner_status == OwnerStatus.OWNER else "ambient_relevant_signal",
        speaker_evidence + evidence,
        ambient_authorized=ambient_authorized,
    )


__all__ = [
    "CATEGORY_ORDER",
    "Category",
    "CorrectionRecord",
    "Disposition",
    "Evidence",
    "InterestModel",
    "InterestWeight",
    "OwnerStatus",
    "Provenance",
    "RelevanceResult",
    "SpeakerContext",
    "content_fingerprint",
    "evaluate_relevance",
]
