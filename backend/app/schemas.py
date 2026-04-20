"""Pydantic schemas used by the HTTP API."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from .models import ProjectStatus, TaskStatus


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1)


class TaskView(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus
    attempts: int
    file_paths: list[str]
    review_notes: str
    last_error: str

    class Config:
        from_attributes = True


class FileView(BaseModel):
    path: str
    revision: int

    class Config:
        from_attributes = True


class EventView(BaseModel):
    id: str
    kind: str
    role: str
    message: str
    data: dict | None = None
    created_at: dt.datetime

    class Config:
        from_attributes = True


class ProjectSummary(BaseModel):
    id: str
    name: str
    status: ProjectStatus
    iteration: int
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


class ProjectDetail(ProjectSummary):
    prompt: str
    workspace_path: str
    zip_path: str
    last_error: str
    tasks: list[TaskView]
    files: list[FileView]

    class Config:
        from_attributes = True


class ServerStatus(BaseModel):
    name: str
    label: str
    url: str
    online: bool
    models: list[str]
    error: str = ""


class ControlRequest(BaseModel):
    action: str  # pause / resume / stop / retry


class CreateNoteRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class NoteView(BaseModel):
    id: str
    content: str
    acknowledged: bool
    created_at: dt.datetime

    class Config:
        from_attributes = True
