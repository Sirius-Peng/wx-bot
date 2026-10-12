-- 微信 LLM 机器人 · 数据库初始化
CREATE EXTENSION IF NOT EXISTS vector;

-- ── 消息归档（全量，审计与分析用）────────────────────────
CREATE TABLE IF NOT EXISTS message_log (
    id          BIGSERIAL PRIMARY KEY,
    msg_id      VARCHAR(128) UNIQUE NOT NULL,
    platform    VARCHAR(32),
    chat_type   VARCHAR(16),
    chat_id     VARCHAR(64),
    sender_id   VARCHAR(64),
    sender_name VARCHAR(128),
    content     TEXT,
    msg_type    VARCHAR(16),
    is_at_me    BOOLEAN,
    direction   VARCHAR(8),      -- in | out
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_msg_chat_time ON message_log(chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_created   ON message_log(created_at DESC);

-- ── 会话元数据 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversation (
    chat_id        VARCHAR(64) PRIMARY KEY,
    chat_type      VARCHAR(16),
    first_seen_at  TIMESTAMPTZ DEFAULT NOW(),
    last_active_at TIMESTAMPTZ DEFAULT NOW(),
    turn_count     INT DEFAULT 0,
    total_tokens   INT DEFAULT 0,
    summary        TEXT
);

-- ── 群配置 ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS group_config (
    chat_id        VARCHAR(64) PRIMARY KEY,
    group_name     VARCHAR(128),
    enabled        BOOLEAN     DEFAULT TRUE,
    mode           VARCHAR(16) DEFAULT 'smart',   -- smart | mention_only | keyword_only
    wake_words     JSONB       DEFAULT '["小助手"]',
    persona        TEXT,
    cooldown_sec   INT         DEFAULT 60,
    hourly_limit   INT         DEFAULT 8,
    reply_prob     REAL        DEFAULT 0.7,
    quiet_hours    JSONB       DEFAULT '[0,1,2,3,4,5,6]',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- ── 用户画像（仅私聊，群聊不建画像）──────────────────────
CREATE TABLE IF NOT EXISTS user_profile (
    user_id      VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128),
    tags         JSONB DEFAULT '[]',
    preferences  JSONB DEFAULT '{}',
    last_seen_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 长期记忆（向量）─────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_vector (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   VARCHAR(64),
    content    TEXT,
    embedding  vector(1024),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mem_vec
    ON memory_vector USING hnsw (embedding vector_cosine_ops);

-- ── LLM 成本统计 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_usage (
    id             BIGSERIAL PRIMARY KEY,
    chat_id        VARCHAR(64),
    model          VARCHAR(64),
    prompt_tok     INT,
    completion_tok INT,
    cost_yuan      NUMERIC(10,6),
    cached_hit     BOOLEAN DEFAULT FALSE,
    latency_ms     INT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON llm_usage(created_at DESC);
