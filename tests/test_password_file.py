"""The database password can live in .password.env instead of inside DATABASE_URL."""

from sql_agent import config
from sql_agent.config import get_settings, with_password

REMOTE = "postgresql://postgres.abcref@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"


def test_with_password_fills_in_and_encodes():
    assert with_password(REMOTE, "p@ss/w:rd#1") == (
        "postgresql://postgres.abcref:p%40ss%2Fw%3Ard%231@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require")
    assert with_password("postgresql://u:already@h:5432/d", "other") == "postgresql://u:already@h:5432/d"
    assert with_password(REMOTE, "") == REMOTE


def test_password_env_file_is_loaded_beside_env(tmp_path, monkeypatch):
    for k in ("DATABASE_URL", "DATABASE_PASSWORD", "AGENT_DATABASE_URL", "AGENT_DB_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"DATABASE_URL={REMOTE}\nAGENT_DB_PASSWORD=a-strong-agent-password-123\n")
    (tmp_path / config.PASSWORD_FILE).write_text("# Supabase database password\nDATABASE_PASSWORD='s3cr3t@pw'\n")
    s = get_settings()
    assert s.database_url == REMOTE.replace("abcref@", "abcref:s3cr3t%40pw@")
    assert s.agent_database_url.startswith("postgresql://nl2sql_agent.abcref:a-strong-agent-password-123@")
