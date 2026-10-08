"""The three-way comparison of one unit: base, local and incoming.

This is the table of spec §7.4. Evidence is known, absent or unknown, and a known
empty value is not absence; anything unknown goes to review, since nothing can be
inferred from a side we never saw.
"""
from dataclasses import dataclass
from typing import Literal

from oms.sources.models import Evidence


@dataclass(frozen=True)
class UnitDecision:
    action: Literal['keep_local', 'already_matches', 'take_incoming', 'review']
    reasons: tuple[str, ...] = ()


def same_value(left: Evidence, right: Evidence) -> bool:
    if left.kind == 'unknown' or right.kind == 'unknown':
        return False
    return left.kind == right.kind and (left.kind == 'absent' or left.value == right.value)


def compare_unit(base: Evidence, local: Evidence, incoming: Evidence, *,
                 independently_owned: bool) -> UnitDecision:
    if base.kind == 'unknown':
        return UnitDecision('review', ('unknown_base',))
    if incoming.kind == 'unknown':
        return UnitDecision('review', ('unknown_incoming',))
    if same_value(incoming, base):
        return UnitDecision('keep_local')
    if local.kind == 'unknown':
        return UnitDecision('review', ('unknown_local',))
    if incoming.kind == 'absent' and local.kind != 'absent':
        return UnitDecision('review', ('deletion_consent',) +
                            (('local_divergence',) if not same_value(local, base) else ()) +
                            (('independent_ownership',) if independently_owned else ()))
    if same_value(local, incoming):
        return UnitDecision('already_matches')
    if independently_owned:
        return UnitDecision('review', ('independent_ownership',))
    if same_value(local, base):
        return UnitDecision('take_incoming')
    return UnitDecision('review', ('local_divergence',))


def compare_field(name: str, base: Evidence, local: Evidence, incoming: Evidence, *,
                  curated: bool = False, independently_owned: bool = False) -> UnitDecision:
    if name in ('name', 'domain'):
        return UnitDecision('keep_local', ('local_identity_preserved',))
    if name not in ('description', 'tags', 'license', 'document_mode'):
        return UnitDecision('keep_local', ('unsupported_field',))
    if name == 'tags':
        def canonical(evidence):
            value = evidence.value
            if evidence.kind == 'known' and isinstance(value, list) and all(isinstance(item, str) for item in value):
                return evidence.model_copy(update={'value': sorted(set(value))})
            return evidence
        base, local, incoming = map(canonical, (base, local, incoming))
    result = compare_unit(base, local, incoming, independently_owned=independently_owned)
    if (name in ('description', 'tags') and curated and not same_value(base, incoming)
            and not same_value(local, incoming)):
        return UnitDecision('review', tuple(dict.fromkeys((*result.reasons, 'curated_field'))))
    return result


def compare_prose(base: Evidence, local: Evidence, incoming: Evidence, *,
                  independently_owned: bool = False, identity_known: bool = True) -> UnitDecision:
    result = compare_unit(base, local, incoming, independently_owned=independently_owned)
    if result.action in ('keep_local', 'already_matches'):
        return result
    if not identity_known:
        return UnitDecision('review', tuple(dict.fromkeys((*result.reasons, 'unknown_identity'))))
    # Relative order is reviewed by the merge plan, where sibling insertions are visible.
    return result


def compare_rule(base: Evidence, local: Evidence, incoming: Evidence, *,
                 independently_owned: bool = False, identity_known: bool = True) -> UnitDecision:
    result = compare_unit(base, local, incoming, independently_owned=independently_owned)
    if result.action in ('keep_local', 'already_matches'):
        return result
    reasons = result.reasons
    if not identity_known:
        reasons += ('unknown_identity',)
    if (local.kind == incoming.kind == 'known' and isinstance(local.value, dict)
            and isinstance(incoming.value, dict)):
        if (local.value.get('polarity') is not None and incoming.value.get('polarity') is not None
                and local.value['polarity'] != incoming.value['polarity']):
            reasons += ('opposite_polarity',)
        if local.value.get('corroboration_count', 1) != 1 and local.value.get('body') != incoming.value.get('body'):
            reasons += ('independent_corroboration',)
    return UnitDecision('review', tuple(dict.fromkeys(reasons))) if reasons else result
