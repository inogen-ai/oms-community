"""Authorisation scopes for the ingestion boundary (Section 8).

Scope strings are kept here so the domain, ports, and adapters share one
source of truth without a circular import.
"""

# The only scope a contributing agent holds: it may write to the ingestion
# queue and nothing else (no promote, publish, or constraint edit; Section 8.3).
INGEST_WRITE = "ingest:write"

# Shared authority vocabulary; each composition supplies its own policy.
INGEST_AUTOWRITE = "ingest:autowrite"

# May make review decisions.
REVIEW_DECIDE = "review:decide"

# May administer access.
ADMINISTER = "admin:administer"

# May submit skill packages for approval.
SKILL_UPLOAD = "skill:upload"

# Constraint-editing scope name; declaring a name does not enforce it.
CONSTRAINT_EDIT = "constraint:edit"


from enum import IntEnum

class Assurance(IntEnum):
    """How well a binding was proven. Ordered so tenant policy can require a
    minimum (§5); IntEnum rather than Enum is what makes `>=` meaningful."""
    ADMIN_ASSERTED = 10     # an administrator minted the token directly
    EMAIL_CHALLENGE = 20    # the human clicked an email magic link
    SSO_SESSION = 30        # the human authenticated against the tenant's IdP
