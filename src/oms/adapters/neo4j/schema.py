"""Core graph constraints and lexical indexes; no model services are required."""
NODE_KEY_CONSTRAINTS = [
    'CREATE CONSTRAINT contribution_lock IF NOT EXISTS FOR (n:ContributionLock) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT workflow_lock IF NOT EXISTS FOR (n:WorkflowLock) REQUIRE n.tenant_id IS UNIQUE',
    'CREATE CONSTRAINT rule_id IF NOT EXISTS FOR (n:Rule) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT txn_id IF NOT EXISTS FOR (n:Transaction) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT tag_id IF NOT EXISTS FOR (n:Tag) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT learning_id IF NOT EXISTS FOR (n:Learning) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT constraint_id IF NOT EXISTS FOR (n:Constraint) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT reviewitem_id IF NOT EXISTS FOR (n:ReviewItem) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT publication_id IF NOT EXISTS FOR (n:Publication) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT usage_event_id IF NOT EXISTS FOR (n:UsageEvent) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT skill_storage_key IF NOT EXISTS FOR (n:Skill) REQUIRE n.storage_key IS UNIQUE',
    'CREATE CONSTRAINT example_storage_key IF NOT EXISTS FOR (n:Example) REQUIRE n.storage_key IS UNIQUE',
    'CREATE CONSTRAINT section_storage_key IF NOT EXISTS FOR (n:Section) REQUIRE n.storage_key IS UNIQUE',
    'CREATE CONSTRAINT content_block_storage_key IF NOT EXISTS FOR (n:ContentBlock) REQUIRE n.storage_key IS UNIQUE',
    'CREATE CONSTRAINT artefact_storage_key IF NOT EXISTS FOR (n:Artefact) REQUIRE n.storage_key IS UNIQUE',
    'CREATE CONSTRAINT skill_version_storage_key IF NOT EXISTS FOR (n:SkillVersion) REQUIRE n.storage_key IS UNIQUE',
]
ENTITY_ID_INDEX = 'CREATE INDEX entity_id IF NOT EXISTS FOR (n:Entity) ON (n.id)'
TRANSACTION_PERSON_INDEX = 'CREATE INDEX transaction_person IF NOT EXISTS FOR (t:Transaction) ON (t.tenant_id, t.person_id)'
FULLTEXT_INDEX = 'CREATE FULLTEXT INDEX rule_fulltext IF NOT EXISTS FOR (n:Rule) ON EACH [n.body]'
SKILL_DOMAIN_INDEX = 'CREATE INDEX skill_domain IF NOT EXISTS FOR (s:Skill) ON (s.tenant_id, s.domain)'
REGISTER_OPTIONAL_PROPERTY_KEYS = ["CALL db.createProperty('compile_status')", "CALL db.createProperty('summary')"]
NODE_KEY_CONSTRAINTS += [
    "CREATE CONSTRAINT artefact_occurrence_key IF NOT EXISTS FOR (n:ArtefactOccurrence) REQUIRE n.storage_key IS UNIQUE",
    "CREATE CONSTRAINT source_record_key IF NOT EXISTS FOR (n:SourceRecord) REQUIRE n.storage_key IS UNIQUE",
]
SOURCE_RECORD_INDEX = "CREATE INDEX source_record_scope IF NOT EXISTS FOR (n:SourceRecord) ON (n.tenant_id,n.kind,n.record_id)"
CORE_STATEMENTS = [SOURCE_RECORD_INDEX] + NODE_KEY_CONSTRAINTS + [ENTITY_ID_INDEX, TRANSACTION_PERSON_INDEX, FULLTEXT_INDEX, SKILL_DOMAIN_INDEX] + REGISTER_OPTIONAL_PROPERTY_KEYS

OBSOLETE_ID_CONSTRAINTS = ('skill_id', 'example_id', 'section_id', 'content_block_id', 'artefact_id')
