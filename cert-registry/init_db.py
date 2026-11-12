"""初始化 SQLite 业务库。用法：python cert-registry/init_db.py"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT / "schema.sql"
DB = ROOT / "registry.db"

sql = SCHEMA.read_text(encoding="utf-8")
conn = sqlite3.connect(DB)
conn.executescript(sql)

# 老库补列：⑦ 的参数卡 knowledge_id
columns = {row[1] for row in conn.execute("PRAGMA table_info(certificate)")}
if "param_card_knowledge_id" not in columns:
    conn.execute("ALTER TABLE certificate ADD COLUMN param_card_knowledge_id TEXT")

conn.commit()
conn.close()
print(f"初始化完成: {DB}")
