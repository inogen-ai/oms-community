"""The local publication settings shared by HTTP, CLI, history and MCP reads."""
from oms.publish.publisher import Publisher


def publication_settings(settings_store, tenant_id):
    stored = settings_store.get_or_seed_default(tenant_id).body
    return {"body_budget": stored.get("body_budget", 200),
            "skill_history_keep": stored.get("skill_history_keep", 200),
            "root_instruction_files": stored.get("root_instruction_files", ["AGENTS.md", "CLAUDE.md"])}


class ConfiguredPublisher(Publisher):
    """Refresh local rendering policy for each render, including Tier 2 reads.

    The contribution and MCP addresses come from the environment rather than the
    settings store, so they are held here and passed into every refreshed
    publisher. Omitting them is what published a bundle with no way to
    contribute back, so they travel with the store settings, not beside them.
    """
    def __init__(self, store, *, settings_store, tenant_id, blob_store,
                 contribution_endpoint=None, mcp_endpoint=None):
        self.settings_store, self.tenant_id = settings_store, tenant_id
        super().__init__(store, blob_store=blob_store,
                         contribution_endpoint=contribution_endpoint,
                         mcp_endpoint=mcp_endpoint)

    def configured(self):
        values = publication_settings(self.settings_store, self.tenant_id)
        return Publisher(self._store, blob_store=self._blob_store,
                         body_budget=values["body_budget"],
                         root_instruction_files=values["root_instruction_files"],
                         contribution_endpoint=self._contribution_endpoint,
                         mcp_endpoint=self._mcp_endpoint)

    def _render(self, *args, **kwargs):
        return self.configured()._render(*args, **kwargs)

    def render_root(self, *args, **kwargs):
        return self.configured().render_root(*args, **kwargs)

    def outline_skill(self, *args, **kwargs):
        return self.configured().outline_skill(*args, **kwargs)

    def publish(self, *args, **kwargs):
        return self.configured().publish(*args, **kwargs)

    def check(self, *args, **kwargs):
        return self.configured().check(*args, **kwargs)
