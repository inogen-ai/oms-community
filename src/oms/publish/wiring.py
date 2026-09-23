"""Local publication composition using already constructed public adapters."""
from dataclasses import dataclass

from oms.ports.blob_store import BlobStore
from oms.ports.graph_store import GraphStore
from oms.ports.publishing import PublishingRenderer
from oms.publish.publisher import Publisher


@dataclass(frozen=True)
class PublicationSettings:
    body_budget: int = 200
    contribution_endpoint: str | None = None
    root_instruction_files: tuple[str, ...] | None = None
    root_rule_formats: tuple[str, ...] | None = None
    mcp_endpoint: str | None = None
    tier2_manifest_path: str | None = "tier2/AGENTS.md"
    bundle_token: str | None = None
    distribution_repo: str | None = None


def build_local_publisher(store: GraphStore, settings: PublicationSettings, *,
                          blob_store: BlobStore | None = None,
                          renderer: PublishingRenderer | None = None) -> Publisher:
    """Construct a publisher without opening a database or reading environment."""
    return Publisher(
        store, body_budget=settings.body_budget, blob_store=blob_store,
        contribution_endpoint=settings.contribution_endpoint,
        root_instruction_files=(list(settings.root_instruction_files)
                                if settings.root_instruction_files is not None else None),
        root_rule_formats=(list(settings.root_rule_formats)
                           if settings.root_rule_formats is not None else None),
        mcp_endpoint=settings.mcp_endpoint,
        tier2_manifest_path=settings.tier2_manifest_path,
        bundle_token=settings.bundle_token,
        distribution_repo=settings.distribution_repo,
        renderer=renderer,
    )
