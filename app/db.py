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
    create_engine, String, Integer, Float, Text, ForeignKey, Index, select
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


class Base(DeclarativeBase):
    pass


class Province(Base):
    __tablename__ = "provinces"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    schools: Mapped[list["School"]] = relationship(back_populates="province")


class School(Base):
    __tablename__ = "schools"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    track: Mapped[str] = mapped_column(String(16), index=True)  # علمي / أدبي / فنون
    province_code: Mapped[str] = mapped_column(ForeignKey("provinces.code"), index=True)
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
    school_code: Mapped[str] = mapped_column(ForeignKey("schools.code"), index=True)
    school: Mapped["School"] = relationship(back_populates="students")

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


if __name__ == "__main__":
    init_db()
    print("initialized:", DATABASE_URL)
