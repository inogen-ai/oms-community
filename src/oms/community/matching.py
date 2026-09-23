"""Small, deterministic review suggestions. Scores are lexical, never semantic proof."""
from difflib import SequenceMatcher
import re
import unicodedata

from oms.domain.types import Plane, RuleStatus


_COMMON = frozenset("a an and are as at be been before by can do does for from has have if in into is it its of on or should that the their them then this to use using was when with you your".split())
_NEGATION = frozenset({"not", "never", "no", "without", "avoid", "cannot", "don't", "mustn't"})


def exact_text(body: str) -> str:
    """Preserve punctuation and meaning while ignoring case and spacing."""
    return " ".join(unicodedata.normalize("NFC", body).casefold().split())


def _terms(body: str) -> frozenset[str]:
    # Retain Unicode and technical terms such as C++/C#. Simple plurals help
    # receipts match receipt without introducing a language model or stemmer.
    tokens = re.findall(r"[^\W_]+(?:['’][^\W_]+)*(?:\+\+|#)?", exact_text(body))
    return frozenset(token[:-1] if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is"))
                     else token for token in tokens if token not in _COMMON)


def _hint_key(value: str) -> str:
    return re.sub(r"[-_\s]+", " ", exact_text(value))[:256]


def _shared_reason(shared: set[str] | frozenset[str], source: str) -> str:
    return f"{source}: " + ", ".join(sorted(shared)[:4]) + "."


class LocalMatcher:
    """Prepare a workspace once per inbox read; never persist a suggestion."""

    def __init__(self, skills, rules):
        self.skills = [(skill, _terms(skill.name), _terms(f"{skill.name} {skill.description or ''}"))
                       for skill in skills]
        self.rules = [(rule, exact_text(rule.body), _terms(rule.body)) for rule in rules
                      if rule.status is RuleStatus.ACTIVE and rule.plane is Plane.DATA]

    def skills_for(self, body: str, hint: str | None, *, limit: int = 5) -> list[dict]:
        query, normal_hint = _terms(body), _hint_key(hint or "")
        ranked = []
        for skill, name_terms, terms in self.skills:
            exact_hint = bool(hint and exact_text(hint) in (exact_text(skill.id), exact_text(skill.name)))
            shared = query & terms
            fuzzy = max((SequenceMatcher(None, normal_hint, _hint_key(value)).ratio()
                         for value in (skill.id, skill.name)), default=0) if len(normal_hint) >= 4 else 0
            if exact_hint:
                group, score, reason = 0, 1.0, "Matches the supplied skill hint."
            elif fuzzy >= 0.72:
                group, score, reason = 1, fuzzy * 0.9, "Name or ID closely resembles the supplied skill hint."
                if shared:
                    reason += " " + _shared_reason(shared, "Correction also shares words")
            elif shared:
                # Coverage favours useful focused matches rather than a long
                # description which happens to contain a single query word.
                score = (len(shared) / max(len(query), 1) + len(shared) / max(len(terms), 1)) / 2
                score += 0.15 * len(query & name_terms) / max(len(name_terms), 1)
                group, score = 2, min(score, 0.89)
                reason = _shared_reason(shared, "Correction shares words with the skill name or description")
            else:
                continue
            ranked.append((group, -score, skill.name.casefold(), skill.id,
                           {"id": skill.id, "name": skill.name, "score": round(score, 3), "reason": reason}))
        return [row[-1] for row in sorted(ranked)[:limit]]

    def rules_for(self, body: str, *, limit: int = 5) -> tuple[list[dict], list[dict]]:
        normal, query = exact_text(body), _terms(body)
        exact, ranked = [], []
        for rule, rule_normal, terms in self.rules:
            is_exact = normal == rule_normal
            shared = query & terms
            if is_exact:
                exact.append({"id": rule.id, "body": rule.body})
                score, reason = 1.0, "Same wording after normalising case and spacing."
            else:
                score = 2 * len(shared) / max(len(query) + len(terms), 1)
                if score < 0.35 or (len(shared) < 2 and min(len(query), len(terms)) > 1):
                    continue
                reason = _shared_reason(shared, "Shares words")
                if (query & _NEGATION) != (terms & _NEGATION):
                    reason += " Negation differs; check whether the meanings conflict."
                else:
                    reason += " Compare the meaning before reinforcing."
            ranked.append((not is_exact, -score, rule.id,
                           {"id": rule.id, "body": rule.body, "score": round(score, 3),
                            "reason": reason, "exact": is_exact}))
        return sorted(exact, key=lambda rule: rule["id"]), [row[-1] for row in sorted(ranked)[:limit]]
