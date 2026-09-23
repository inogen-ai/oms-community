"""Content-addressed ID conventions shared by every writer.

Every content writer must produce byte-identical IDs for the same content so
re-imports and repeated decisions remain idempotent.
"""
import hashlib
import re


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalise(text: str) -> str:
    text = text.strip().lower().rstrip(".")
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def block_id(section_id: str, content_hash: str) -> str:
    return f"block-{section_id}-{content_hash[:16]}"


def example_id(parent_id: str, body: str) -> str:
    return "example-" + hashlib.sha1(
        f"{parent_id}|{normalise(body)}".encode("utf-8")).hexdigest()[:16]


def constraint_id(tenant_id: str, body: str, *, console: bool = False) -> str:
    """A constraint's id, derived from its text so re-writing one is a no-op.

    Content-addressed for the reason block ids are: the importer re-reads
    CLAUDE.md on every run, and an id built from anything else would mint a
    second copy of the same constraint each time.

    `console` puts a marker in the middle, and that segment is doing real work.
    An imported constraint is a projection of a file in the source tree, so the
    console must not edit it - the next import would undo the edit. Without the
    marker, an administrator typing a body that happened to match an imported
    one would land on that constraint's id, the MERGE would relabel it
    `source: console`, and the console would then offer a Retire button that
    silently un-presses itself at the next import. Separate namespaces make
    that collision impossible rather than merely unlikely.

    The console form also carries a hash of the whole body, and the imported
    form does not. That asymmetry is deliberate on both sides.

    The hash is there because a 32-character slug is about five English words,
    and constraints are sentences that nearly all begin "Never ..." or
    "Always ...". "Never store a customer's payment details in plain text." and
    "Never store a customer's payment details, ever, anywhere." share their
    first 32 slug characters exactly, so on the slug alone they are one id -
    and `upsert_constraint` MERGEs, so writing the second SILENTLY DESTROYS the
    first, retired record and all. That breaks this module's neighbours'
    promise that a constraint is retired and never deleted "because it is why
    some rule was rejected". It also fixes a worse case: `slug` strips
    everything but ASCII letters and digits, so a body written in Cyrillic or
    Chinese, or one made only of punctuation, slugs to the empty string. Every
    such constraint in a tenant would share the id `constraint-{tenant}-console-`
    and a tenant could hold exactly one of them.

    The IMPORTED form keeps its old shape byte for byte because it is already
    in every deployed graph. Adding the hash there would mint a second node for
    every constraint on the next import, leaving the originals orphaned and
    still binding - a migration, not a bug fix. It carries the same truncation
    risk, at much lower odds: those bodies come from a file somebody reviewed,
    not from a free-text box, and a collision there is at least visible in the
    source tree. Fixing it needs a migration and belongs in its own change.

    The hash input is folded here rather than by `normalise`, and that is not
    a stylistic difference. `normalise` drops every character outside
    `[a-z0-9 ]`, which is right for the block and example ids it was written
    for - English prose out of a reviewed source tree - and catastrophic here:
    a Cyrillic body and a Chinese body would BOTH normalise to the empty
    string and hash identically, which is the same one-constraint-per-tenant
    fault the stem has, moved into the part that was supposed to fix it.
    Folding case and whitespace keeps "Never store PII." and "never store pii"
    one constraint without throwing away the writing system.
    """
    stem = slug(body)[:32]
    if not console:
        return f"constraint-{tenant_id}-{stem}"
    folded = " ".join(body.strip().rstrip(".").lower().split())
    digest = hashlib.sha1(folded.encode("utf-8")).hexdigest()[:12]
    # No stem at all for a body that slugs to nothing, rather than a dangling
    # hyphen: `constraint-acme-console--ab12...` reads as a missing field.
    return (f"constraint-{tenant_id}-console-{stem}-{digest}" if stem
            else f"constraint-{tenant_id}-console-{digest}")
