import pytest
from sqlalchemy import Engine

from src.database import get_engine, Base


@pytest.fixture
def db_engine(tmp_path) -> Engine:
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine
