-- JARVIS initial schema (Spec §64).
-- Migrations are forward-only and additive; the database is never dropped to apply a change.

-- The single local user profile. JARVIS is a personal assistant, but keeping a row here
-- means preferences and pairing can later be scoped per profile without a schema rewrite.
CREATE TABLE local_profile (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    display_name    TEXT    NOT NULL DEFAULT 'User',
    timezone        TEXT    NOT NULL DEFAULT 'UTC',
    locale          TEXT    NOT NULL DEFAULT 'de-DE',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO local_profile (id) VALUES (1);

CREATE TABLE projects (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    description         TEXT    NOT NULL DEFAULT '',
    workspace_path      TEXT,
    repository          TEXT,
    branch              TEXT,
    custom_instructions TEXT    NOT NULL DEFAULT '',
    integrations        TEXT    NOT NULL DEFAULT '[]',   -- JSON array of integration names
    color               TEXT    NOT NULL DEFAULT 'ice',
    archived            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE project_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    role        TEXT    NOT NULL DEFAULT 'reference',   -- reference | entrypoint | config | doc
    notes       TEXT    NOT NULL DEFAULT '',
    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, path)
);

CREATE TABLE conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL DEFAULT 'Neue Unterhaltung',
    project_id    INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    source        TEXT    NOT NULL DEFAULT 'text',      -- text | voice | mobile | automation
    model_id      TEXT,
    pinned        INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
CREATE INDEX idx_conversations_project ON conversations(project_id);

CREATE TABLE messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT    NOT NULL,                  -- system | user | assistant | tool
    content          TEXT    NOT NULL DEFAULT '',
    -- Trust boundary marker (Spec §90): where this content came from, so external text is
    -- never treated as an instruction.
    trust            TEXT    NOT NULL DEFAULT 'user',   -- system|user|memory|external|tool_result
    model_id         TEXT,
    agent            TEXT,
    tool_calls       TEXT,                              -- JSON array
    attachments      TEXT,                              -- JSON array
    citations        TEXT,                              -- JSON array (Spec §44)
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    cost_usd         REAL,
    error            TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL DEFAULT 'fact',       -- preference|project|entity|task|fact|workflow
    subject      TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL,
    importance   REAL    NOT NULL DEFAULT 0.5,
    confidence   REAL    NOT NULL DEFAULT 0.8,
    source       TEXT    NOT NULL DEFAULT 'conversation',
    project_id   INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    pinned       INTEGER NOT NULL DEFAULT 0,
    disabled     INTEGER NOT NULL DEFAULT 0,
    hits         INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_memories_kind ON memories(kind, disabled);
CREATE INDEX idx_memories_importance ON memories(importance DESC);

-- Full-text index over memories, kept in sync by triggers (Spec §65 — FTS5, no paid API).
CREATE VIRTUAL TABLE memories_fts USING fts5(
    subject, content, content='memories', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, subject, content)
    VALUES ('delete', old.id, old.subject, old.content);
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, subject, content)
    VALUES ('delete', old.id, old.subject, old.content);
    INSERT INTO memories_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;

CREATE TABLE tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    goal          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'PENDING',
    intent        TEXT,
    project_id    INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    parent_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    agent         TEXT,
    progress      REAL    NOT NULL DEFAULT 0.0,
    result        TEXT,
    error         TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX idx_tasks_status ON tasks(status, created_at DESC);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);

CREATE TABLE task_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    title       TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'PENDING',
    agent       TEXT,
    detail      TEXT    NOT NULL DEFAULT '',
    result      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX idx_task_steps_task ON task_steps(task_id, position);

CREATE TABLE reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    text           TEXT    NOT NULL,
    schedule_kind  TEXT    NOT NULL DEFAULT 'one_time',
    -- Stored as UTC ISO-8601; the originating timezone is kept so recurring reminders stay
    -- anchored to local wall-clock time across DST changes (Spec §27).
    next_run_at    TEXT    NOT NULL,
    timezone       TEXT    NOT NULL DEFAULT 'UTC',
    interval_seconds INTEGER,
    weekdays       TEXT,                                -- JSON array of 0-6, Monday = 0
    day_of_month   INTEGER,
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_fired_at  TEXT,
    fire_count     INTEGER NOT NULL DEFAULT 0,
    missed_count   INTEGER NOT NULL DEFAULT 0,
    project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_reminders_next ON reminders(enabled, next_run_at);

CREATE TABLE automations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,
    description    TEXT    NOT NULL DEFAULT '',
    trigger_kind   TEXT    NOT NULL DEFAULT 'schedule', -- schedule | condition | event
    schedule_kind  TEXT,
    next_run_at    TEXT,
    interval_seconds INTEGER,
    weekdays       TEXT,
    timezone       TEXT    NOT NULL DEFAULT 'UTC',
    condition      TEXT,                                -- JSON condition descriptor
    actions        TEXT    NOT NULL DEFAULT '[]',       -- JSON array of tool/agent steps
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_run_at    TEXT,
    last_status    TEXT,
    last_result    TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE voice_commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase          TEXT    NOT NULL UNIQUE,
    aliases         TEXT    NOT NULL DEFAULT '[]',
    action_kind     TEXT    NOT NULL DEFAULT 'tool',    -- tool | command | workflow | prompt
    action          TEXT    NOT NULL DEFAULT '{}',      -- JSON payload for the action
    working_directory TEXT,
    permission      TEXT    NOT NULL DEFAULT 'ASK',
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_used_at    TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE app_registry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL,
    aliases     TEXT    NOT NULL DEFAULT '[]',
    path        TEXT,
    args        TEXT    NOT NULL DEFAULT '[]',
    kind        TEXT    NOT NULL DEFAULT 'application', -- application | url | script
    detected    INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE tool_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tool          TEXT    NOT NULL,
    agent         TEXT,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    arguments     TEXT    NOT NULL DEFAULT '{}',        -- redacted JSON
    risk_level    TEXT    NOT NULL DEFAULT 'SAFE_READ',
    permission    TEXT    NOT NULL DEFAULT 'ASK',
    decision      TEXT    NOT NULL DEFAULT 'allowed',
    status        TEXT    NOT NULL DEFAULT 'started',   -- started | success | failed | denied
    result_summary TEXT   NOT NULL DEFAULT '',
    error         TEXT,
    duration_ms   INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_tool_runs_created ON tool_runs(created_at DESC);

CREATE TABLE agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    role         TEXT    NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    model_id     TEXT,                                   -- NULL means "let the router decide"
    priorities   TEXT    NOT NULL DEFAULT '[]',
    system_prompt TEXT   NOT NULL DEFAULT '',
    run_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE integrations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    status        TEXT    NOT NULL DEFAULT 'Disconnected',
    config        TEXT    NOT NULL DEFAULT '{}',        -- non-secret configuration only
    last_checked_at TEXT,
    last_error    TEXT,
    enabled       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE devices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    platform       TEXT    NOT NULL DEFAULT 'unknown',
    token_hash     TEXT    NOT NULL UNIQUE,             -- only the hash is stored
    scopes         TEXT    NOT NULL DEFAULT '[]',
    paired_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at   TEXT,
    last_ip        TEXT,
    revoked        INTEGER NOT NULL DEFAULT 0,
    expires_at     TEXT
);

CREATE TABLE settings_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT    NOT NULL,
    agent       TEXT,
    tool        TEXT,
    permission  TEXT,
    decision    TEXT,
    result      TEXT,
    detail      TEXT    NOT NULL DEFAULT '{}',          -- redacted JSON
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);

CREATE TABLE knowledge_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT '',
    source_kind TEXT    NOT NULL DEFAULT 'file',        -- file | url | note
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    content_hash TEXT   NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE knowledge_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    content     TEXT    NOT NULL,
    heading     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX idx_chunks_document ON knowledge_chunks(document_id, position);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    heading, content, content='knowledge_chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(rowid, heading, content) VALUES (new.id, new.heading, new.content);
END;
CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, heading, content)
    VALUES ('delete', old.id, old.heading, old.content);
END;
CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, heading, content)
    VALUES ('delete', old.id, old.heading, old.content);
    INSERT INTO knowledge_fts(rowid, heading, content) VALUES (new.id, new.heading, new.content);
END;

-- Cached provider model catalogue (Spec §7 — dynamic, never a hardcoded list).
CREATE TABLE model_catalog (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT    NOT NULL,
    model_id          TEXT    NOT NULL,
    name              TEXT    NOT NULL DEFAULT '',
    context_length    INTEGER,
    price_prompt      TEXT,                             -- NULL means Unknown, never guessed
    price_completion  TEXT,
    is_free           INTEGER NOT NULL DEFAULT 0,
    supports_tools    INTEGER,
    supports_vision   INTEGER,
    supports_structured INTEGER,
    supports_reasoning  INTEGER,
    raw               TEXT    NOT NULL DEFAULT '{}',
    refreshed_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, model_id)
);
CREATE INDEX idx_model_catalog_free ON model_catalog(is_free, provider);
