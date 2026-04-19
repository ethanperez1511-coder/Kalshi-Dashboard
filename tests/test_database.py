from sqlalchemy import text
from src.database import get_engine, get_session, Base


def test_engine_creates_sqlite_database(tmp_path):
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    assert engine is not None


def test_session_can_execute_query(tmp_path):
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with get_session(engine) as session:
        result = session.execute(text("SELECT 1"))
        assert result.scalar() == 1
