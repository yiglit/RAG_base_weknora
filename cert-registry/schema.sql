PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS certificate (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  cert_no       TEXT NOT NULL,
  institution   TEXT NOT NULL,
  manufacturer  TEXT,
  product_name  TEXT,
  model_spec    TEXT,
  ex_marking    TEXT,
  standard      TEXT,
  drawing_no    TEXT,
  issue_date    TEXT,
  valid_until   TEXT,
  weknora_kb_id TEXT,
  weknora_knowledge_id TEXT,
  -- ⑦ 回写知识库后，参数卡（manual 文档）自己的 knowledge_id。
  -- weknora_knowledge_id 保留给「原件 PDF」，⑩ 出处校验靠它把 knowledge_id 映射回证书编号。
  param_card_knowledge_id TEXT,
  raw_json      TEXT,
  confidence_min REAL,
  review_status TEXT DEFAULT 'pending',
  created_at    TEXT DEFAULT (datetime('now')),
  updated_at    TEXT DEFAULT (datetime('now')),
  UNIQUE (cert_no, institution)
);

CREATE TABLE IF NOT EXISTS product (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cert_id INTEGER NOT NULL REFERENCES certificate(id) ON DELETE CASCADE,
  name TEXT,
  model TEXT,
  voltage TEXT,
  current TEXT,
  enclosure TEXT
);

CREATE TABLE IF NOT EXISTS drawing (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cert_id INTEGER NOT NULL REFERENCES certificate(id) ON DELETE CASCADE,
  drawing_no TEXT,
  version TEXT
);

CREATE TABLE IF NOT EXISTS inspection_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cert_id INTEGER NOT NULL REFERENCES certificate(id) ON DELETE CASCADE,
  report_no TEXT,
  issued_date TEXT,
  conclusion TEXT
);

CREATE TABLE IF NOT EXISTS import_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cert_no TEXT,
  institution TEXT,
  action TEXT,
  changed_fields TEXT,
  source_file TEXT,
  acted_at TEXT DEFAULT (datetime('now'))
);
