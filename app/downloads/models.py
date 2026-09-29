from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .filenames import relative_path

TaskStatus = Literal[
    "pending", "queued", "downloading", "paused", "completed", "failed", "canceled"
]
TaskType = Literal["comic_chapter", "torrent"]


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    save_path: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Group name must not be blank")
        return value.strip()

    @field_validator("save_path")
    @classmethod
    def validate_path(cls, value):
        return relative_path(value)


class GroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    save_path: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value or not value.strip():
            raise ValueError("Group name must not be blank")
        return value.strip()

    @field_validator("description")
    @classmethod
    def description_not_null(cls, value):
        if value is None:
            raise ValueError("Description must be a string")
        return value

    @field_validator("save_path")
    @classmethod
    def validate_path(cls, value):
        return relative_path(value)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: TaskType
    source: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    comic_id: str | None = Field(default=None, min_length=1, max_length=500)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=500)
    torrent_url: str | None = Field(default=None, max_length=4000)
    group_id: str | None = None
    output_format: Literal["cbz", "file"] | None = None

    @model_validator(mode="after")
    def validate_kind(self):
        expected = "cbz" if self.type == "comic_chapter" else "file"
        self.output_format = self.output_format or expected
        if self.output_format != expected:
            raise ValueError(f"{self.type} only supports {expected}")
        if self.type == "comic_chapter" and (not self.comic_id or not self.chapter_id):
            raise ValueError("Comic downloads require comic_id and chapter_id")
        if self.type == "comic_chapter" and self.torrent_url is not None:
            raise ValueError("Comic downloads cannot contain torrent_url")
        if self.type == "torrent" and (self.comic_id or self.chapter_id):
            raise ValueError("Torrent downloads cannot contain chapter parameters")
        return self

    def idempotency_key(self) -> str:
        identity = [self.type, self.source, self.output_format]
        identity += (
            [self.comic_id, self.chapter_id]
            if self.type == "comic_chapter"
            else [self.source_id]
        )
        return hashlib.sha256(
            json.dumps(identity, ensure_ascii=True).encode()
        ).hexdigest()

    def payload(self) -> dict:
        keys = (
            ("comic_id", "chapter_id")
            if self.type == "comic_chapter"
            else ("torrent_url",)
        )
        return {
            key: getattr(self, key) for key in keys if getattr(self, key) is not None
        }


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=100)
    source_id: str | None = None
    comic_id: str = Field(min_length=1, max_length=500)
    chapter_group: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=500)
    group_id: str | None = None
    output_format: Literal["cbz"] = "cbz"
