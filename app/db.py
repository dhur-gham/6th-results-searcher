"""
Database layer. Defaults to SQLite (zero-config, runs immediately).
Set DATABASE_URL (e.g. postgresql+psycopg://user:pass@host/db) to use Postgres
for the shared production service — no code changes needed.

Data is write-once / read-many (results never change once ingested), so the
search columns are indexed and every read is a single indexed lookup.
"""
import os
import json

from sqlalchemy import (
    create_engine, String, Integer, Float, Text, ForeignKey,
    ForeignKeyConstraint, Index, select,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
)

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DEFAULT_SQLITE = "sqlite:///" + os.path.abspath(os.path.join(DATA_DIR, "results.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# SQLite performance pragmas: WAL avoids the big rollback-journal rewrite per
# transaction (huge on slow/virtualized filesystems), NORMAL sync is safe under
# WAL, and generous cache/mmap keep the working set in memory. Big win for bulk
# ingest. Applied on every new connection.
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=60000")   # wait up to 60s for the write lock

        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-131072")   # ~128 MB page cache
        cur.execute("PRAGMA mmap_size=268435456")  # 256 MB mmap
        cur.close()


class Base(DeclarativeBase):
    pass


class Province(Base):
    __tablename__ = "provinces"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    schools: Mapped[list["School"]] = relationship(back_populates="province")


class School(Base):
    __tablename__ = "schools"
    # School codes are only unique WITHIN a province (e.g. 55051 "externals"
    # repeats in every province), so identity is (province_code, code).
    province_code: Mapped[str] = mapped_column(ForeignKey("provinces.code"), primary_key=True)
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    track: Mapped[str] = mapped_column(String(16), index=True)  # علمي / أدبي / فنون
    province: Mapped["Province"] = relationship(back_populates="schools")
    students: Mapped[list["Student"]] = relationship(back_populates="school")


class Student(Base):
    __tablename__ = "students"
    exam_no: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    name_norm: Mapped[str] = mapped_column(String(128), index=True)  # normalized for search
    result: Mapped[str | None] = mapped_column(String(16))
    total: Mapped[int | None] = mapped_column(Integer)
    average: Mapped[float | None] = mapped_column(Float)
    grades_json: Mapped[str] = mapped_column(Text, default="{}")
    province_code: Mapped[str] = mapped_column(String(8), index=True)
    school_code: Mapped[str] = mapped_column(String(16), index=True)
    school: Mapped["School"] = relationship(back_populates="students")
    __table_args__ = (
        ForeignKeyConstraint(
            ["province_code", "school_code"],
            ["schools.province_code", "schools.code"],
        ),
    )

    @property
    def grades(self) -> dict:
        return json.loads(self.grades_json or "{}")

    def to_dict(self) -> dict:
        return {
            "exam_no": self.exam_no,
            "name": self.name,
            "result": self.result,
            "total": self.total,
            "average": self.average,
            "grades": self.grades,
            "school": {"code": self.school_code,
                       "name": self.school.name if self.school else None,
                       "track": self.school.track if self.school else None,
                       "province": self.school.province.name if self.school and self.school.province else None},
        }


def init_db():
    Base.metadata.create_all(engine)


def reset_all():
    """Delete ALL data (students -> schools -> provinces) and start fresh.

    Irreversible. Used when a new results stage (e.g. الدور الثاني) replaces the
    old dataset. Deletes in FK-safe order and returns the removed row counts.
    Schema is kept (tables remain), so a subsequent ingest just refills them.
    """
    from sqlalchemy import delete, func
    init_db()
    with SessionLocal() as db:
        counts = {
            "students": db.execute(select(func.count(Student.exam_no))).scalar() or 0,
            "schools": db.execute(
                select(func.count()).select_from(School)).scalar() or 0,
            "provinces": db.execute(
                select(func.count()).select_from(Province)).scalar() or 0,
        }
        # children first to satisfy foreign keys
        db.execute(delete(Student))
        db.execute(delete(School))
        db.execute(delete(Province))
        db.commit()
    # reclaim space on SQLite (no-op-ish on Postgres autovacuum)
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
    return counts


if __name__ == "__main__":
    init_db()
    print("initialized:", DATABASE_URL)
