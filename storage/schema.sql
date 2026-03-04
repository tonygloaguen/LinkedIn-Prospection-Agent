-- storage/schema.sql — executed by database.py at startup

CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,              -- SHA256 court de linkedin_url
    linkedin_url TEXT UNIQUE NOT NULL,
    full_name TEXT,
    headline TEXT,
    bio TEXT,
    location TEXT,
    connections_count INTEGER,
    is_recruiter INTEGER DEFAULT 0,
    is_technical INTEGER DEFAULT 0,
    score_recruiter REAL DEFAULT 0.0,
    score_technical REAL DEFAULT 0.0,
    score_activity REAL DEFAULT 0.0,
    score_total REAL DEFAULT 0.0,
    profile_category TEXT,            -- ENUM: recruiter|technical|cto_ciso|other
    scraped_at TEXT,
    last_action TEXT,
    status TEXT DEFAULT 'pending'     -- pending|messaged|connected|ignored
);

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    author_profile_id TEXT,
    content_snippet TEXT,
    post_url TEXT,
    keywords_matched TEXT,            -- JSON array
    found_at TEXT,
    FOREIGN KEY (author_profile_id) REFERENCES profiles(id)
);

CREATE TABLE IF NOT EXISTS action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action_type TEXT NOT NULL,        -- search|scrape|score|message|connect|error
    profile_id TEXT,
    post_id TEXT,
    payload TEXT,                     -- JSON
    success INTEGER DEFAULT 1,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE,
    started_at TEXT,
    ended_at TEXT,
    metrics TEXT                      -- JSON RunMetrics
);
