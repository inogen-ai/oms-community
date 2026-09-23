"""Community composition: adapters are selected here, outside the HTTP factory."""
from dataclasses import dataclass, fields
from pathlib import Path

from oms.community.principal import LocalPrincipalResolver, NullNotifier
from oms.community.repository import MemoryWorkflowRepository, Neo4jWorkflowRepository
from oms.community.workflow import ManualReviewDisposition, ManualReviewService
from oms.community.import_review import ImportReviewService
from oms.ingestion.coordinator import ContributionCoordinator
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.service import IngestionService
from oms.import_skills.importer import SkillImporter
from oms.ports.blob_store import BlobStore
from oms.ports.catalogue_policy import SingleWorkspaceReadPolicy
from oms.ports.custody import RetentionDisabled
from oms.ports.graph_store import GraphStore
from oms.ports.review_queue import ReviewQueue
from oms.ports.settings_store import SettingsStore
from oms.ports.workflow import WorkflowRepository
from oms.publish.catalogue import SkillCatalogue
from oms.publish.publisher import Publisher
from oms.security.injection import DeterministicScreen
from oms.settings.core import CoreSettings
from oms.settings.publication import ConfiguredPublisher, publication_settings
from oms.skills.history import SkillHistory
from oms.skills.service import SkillAdminService
from oms.skills.upload import UploadService


@dataclass(frozen=True)
class CoreServices:
    store: GraphStore
    queue: ReviewQueue
    repository: WorkflowRepository
    coordinator: ContributionCoordinator
    manual: ManualReviewService
    import_review: ImportReviewService
    importer: SkillImporter
    uploads: UploadService
    skills: SkillAdminService
    history: SkillHistory
    publisher: Publisher
    catalogue: SkillCatalogue
    settings_store: SettingsStore
    principal_resolver: LocalPrincipalResolver
    notifier: NullNotifier
    retention: RetentionDisabled
    settings: CoreSettings
    blobs: BlobStore
    payloads: FilePayloadStore

    def validate(self):
        for field in fields(self):
            if getattr(self, field.name) is None:
                raise ValueError(f"required core service is missing: {field.name}")
        methods = {
            "store": ("get_skill", "upsert_rule", "lineage"), "queue": ("pending", "resolve"),
            "repository": ("atomic", "undisposed"), "coordinator": ("ingest", "reconcile"),
            "manual": ("inbox", "decide"), "importer": ("import_directory",),
            "import_review": ("inbox", "decide"),
            "uploads": ("stage", "apply"), "skills": ("create_skill", "sections"),
            "history": ("capture_required",), "publisher": ("publish", "check"),
            "catalogue": ("list_skills", "query_skill", "query_skill_resource"),
            "settings_store": ("get_or_seed_default", "update"),
            "principal_resolver": ("resolve",), "notifier": ("notify",),
            "retention": ("forget",), "blobs": ("get", "put", "exists"), "payloads": ("get", "put"),
        }
        for service, names in methods.items():
            if any(not callable(getattr(getattr(self, service), name, None)) for name in names):
                raise ValueError(f"core service does not implement its contract: {service}")
        if not callable(getattr(getattr(self.coordinator, "disposition", None), "accept", None)):
            raise ValueError("contribution disposition is required")
        self.settings.validate()
        return self


def _compose(store, queue, repository, settings_store, settings):
    from oms.adapters.blobs.file_blob_store import FileBlobStore
    from oms.adapters.vocabulary.in_memory import InMemoryVocabularyStore
    data = Path(settings.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    payloads = FilePayloadStore(data / "payloads")
    blobs = FileBlobStore(data)
    retention = RetentionDisabled()
    # One source for every publisher built here, so a bundle cannot be rendered
    # with the contribution instruction in one path and without it in another.
    endpoints = {"contribution_endpoint": settings.contribution_endpoint,
                 "mcp_endpoint": settings.mcp_endpoint}
    def history_for(repository_store):
        return SkillHistory(store=repository_store,
            publisher=ConfiguredPublisher(repository_store, settings_store=settings_store,
                                           tenant_id=settings.tenant_id, blob_store=blobs,
                                           **endpoints),
            keep=lambda tenant: publication_settings(settings_store, tenant)["skill_history_keep"])
    publisher = ConfiguredPublisher(store, settings_store=settings_store,
                                    tenant_id=settings.tenant_id, blob_store=blobs,
                                    **endpoints)
    history = history_for(store)
    sanitiser = RegexSanitiser()
    ingestion = IngestionService(store, sanitiser, payloads,
        injection_screen=DeterministicScreen(), custody=retention)
    coordinator = ContributionCoordinator(ingestion,
        ManualReviewDisposition(repository, payloads), repository)
    vocabulary = InMemoryVocabularyStore()
    vocabulary.get_or_seed_default(settings.tenant_id).body["domain_extractor"] = "explicit_or_general"
    importer = SkillImporter(store, sanitiser, payloads, queue, blob_store=blobs,
        vocabulary_store=vocabulary, injection_screen=DeterministicScreen(), history=history,
        repository=repository)
    return CoreServices(store, queue, repository, coordinator,
        ManualReviewService(repository, payloads, retention, blob_store=blobs,
                            history_factory=history_for,
                            publisher_factory=lambda repository_store: ConfiguredPublisher(
                                repository_store, settings_store=settings_store,
                                tenant_id=settings.tenant_id, blob_store=blobs,
                                **endpoints)), ImportReviewService(repository, history_for), importer,
        UploadService(store=store, importer=importer, staging_root=data / "uploads", sanitiser=sanitiser),
        SkillAdminService(store=store), history, publisher,
        SkillCatalogue(store, publisher, blob_store=blobs,
                       read_policy=SingleWorkspaceReadPolicy(settings.tenant_id)),
        settings_store, LocalPrincipalResolver(settings.tenant_id), NullNotifier(), retention,
        settings, blobs, payloads).validate()


def build_memory(data_dir: Path, tenant="local") -> CoreServices:
    """Disposable explicit adapter choice for tests and examples."""
    from oms.adapters.memory.store import InMemoryGraphStore
    from oms.adapters.memory.review_queue import InMemoryReviewQueue
    from oms.adapters.memory.settings import InMemorySettingsStore
    store, queue = InMemoryGraphStore(), InMemoryReviewQueue()
    return _compose(store, queue, MemoryWorkflowRepository(store, queue),
        InMemorySettingsStore(), CoreSettings(data_dir=Path(data_dir), tenant_id=tenant))


def build_community(settings: CoreSettings, driver) -> CoreServices:
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
    from oms.adapters.neo4j.settings import Neo4jSettingsStore
    settings.validate()
    store = Neo4jGraphStore(driver)
    store.ensure_schema()  # ownership refusal precedes every other write
    queue = Neo4jReviewQueue(driver)
    services = _compose(store, queue, Neo4jWorkflowRepository(driver, store, queue),
                        Neo4jSettingsStore(driver), settings)
    services.coordinator.reconcile(settings.tenant_id)
    return services
