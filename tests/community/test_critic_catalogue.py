"""Independent checks for the public catalogue's explicit workspace policy."""
import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.ports.catalogue_policy import SingleWorkspaceReadPolicy
from oms.publish.catalogue import SkillCatalogue, SkillNotFound
from oms.publish.publisher import Publisher


@pytest.mark.parametrize("operation", ["list", "skill", "resource"])
def test_denied_workspace_is_refused_before_its_gate_reasons_are_read(operation):
    class TenantGuardStore(InMemoryGraphStore):
        def get_publish_block(self, tenant_id):
            assert tenant_id == "acme", "read a denied workspace's private gate reasons"
            return super().get_publish_block(tenant_id)

        def skills_for_tenant(self, tenant_id):
            assert tenant_id == "acme", "read a denied workspace's skills"
            return super().skills_for_tenant(tenant_id)

    store = TenantGuardStore()
    store.set_publish_block("other", ["Private safety assessment"], source="compile")
    catalogue = SkillCatalogue(store, Publisher(store), read_policy=SingleWorkspaceReadPolicy("acme"))

    if operation == "list":
        assert catalogue.list_skills("other") == []
    else:
        with pytest.raises(SkillNotFound):
            if operation == "skill":
                catalogue.query_skill("other", "withheld")
            else:
                catalogue.query_skill_resource("other", "withheld", "references.md")
