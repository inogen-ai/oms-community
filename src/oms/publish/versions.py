"""One skill's current version, in the one place three surfaces can share.

`render.version_of` takes the digest of whatever bytes it is handed, so the
question "which bytes" is answered separately by every caller that needs a
version - and there are now three: the Tier 2 catalogue's listing, the Tier 2
manifest, and the digest a mute records so the portal can say a skill has been
rewritten since. Three spellings of "render the fetch view and hash it" would
drift the first time one of them was changed, and the symptom would be a
staleness notice that fires on every skill or on none.

Kept out of `render` because it needs a Publisher, and `render` is the module
the Publisher imports.
"""
from oms.domain.models import Skill
from oms.publish.publisher import Publisher
from oms.publish.render import version_of


def skill_version(publisher: Publisher, skill: Skill) -> str:
    """The digest Tier 2 serves this skill's body under.

    The FETCH view, not the published file: the two differ only in how a
    pointer to a reference file is written, but they differ, and a mute stamped
    with one digest and compared against the other would read as stale the
    moment it was made.
    """
    body, _resources = publisher.fetch_view(skill, publisher.render_package(skill))
    return version_of(body)
