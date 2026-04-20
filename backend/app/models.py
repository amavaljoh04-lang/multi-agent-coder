"""SQLAlchemy models.

The DB is the persistence layer that lets a project run for days or weeks:
every task, every file revision, every test iteration is checkpointed so
the worker can crash or the machine can reboot and resume where it left off.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ProjectStatus(enum.StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    ARCHITECTING = "architecting"
    CODING = "coding"
    REVIEWING = "reviewing"
    TESTING = "testing"
    FIXING = "fixing"
    PACKAGING = "packaging"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    CODING = "coding"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(Enum(ProjectStatus), default=ProjectStatus.CREATED)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    workspace_path: Mapped[str] = mapped_column(String(500), default="")
    zip_path: Mapped[str] = mapped_column(String(500), default="")
    architecture: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    heartbeat_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tasks: Mapped[list[Task]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Task.order_index"
    )
    files: Mapped[list[ProjectFile]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Event.created_at"
    )
    test_runs: Mapped[list[TestRun]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="TestRun.created_at"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    file_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    project: Mapped[Project] = relationship(back_populates="tasks")


class ProjectFile(Base):
    __tablename__ = "project_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    project: Mapped[Project] = relationship(back_populates="files")


class ProjectNote(Base):
    """A note posted by the user while the project is running.

    Notes are injected into the agents' prompts on their next iteration so
    the user can nudge the pipeline mid-flight ("use tailwind", "don't use
    pydantic", "the last fix was wrong, revert it") without deleting and
    restarting the project.
    """

    __tablename__ = "project_notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    acknowledged: Mapped[bool] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Event(Base):
    """A stream of log lines / state transitions displayed in the UI."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(50))  # info / agent / test / error / state
    role: Mapped[str] = mapped_column(String(50), default="")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship(back_populates="events")


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    command: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int] = mapped_column(Integer, default=-1)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    analysis: Mapped[str] = mapped_column(Text, default="")
    passed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship(back_populates="test_runs")
