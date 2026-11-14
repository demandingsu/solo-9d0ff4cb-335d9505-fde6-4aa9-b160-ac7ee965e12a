"""Pydantic response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EventOut(BaseModel):
    event_no: str
    event_ts: str
    parent_part_no: str
    child_part_no: str
    child_batch: str
    action: str
    upload_batch: int


class PathStep(BaseModel):
    event_no: str
    event_ts: str
    action: str
    parent_part_no: str
    child_part_no: str
    child_batch: str


class AssemblyPath(BaseModel):
    root_part_no: str = Field(..., description="Top of the chain (P- serial for a product)")
    root_batch: str
    is_finished_product: bool
    path: list[PathStep] = Field(..., description="INSTALL/REMOVE events, child -> root")


class TraceResponse(BaseModel):
    queried_at: str
    child_part_no: str
    child_batch: str
    affected_products: list[AssemblyPath] = Field(
        ..., description="Finished products (P-*) still containing the batch at the queried moment"
    )
    open_chains: list[AssemblyPath] = Field(
        ..., description="Chains whose top assembly is not a finished product"
    )
    total_affected_products: int


class UploadResponse(BaseModel):
    accepted: bool
    filename: str
    received_events: int
    new_events: int
    duplicate_events: int
    message: str


class RejectedUpload(BaseModel):
    accepted: bool
    filename: str
    received_events: int
    reason: str
    errors: list[str]


class BatchOut(BaseModel):
    id: int
    filename: str
    received_at: str
    accepted: bool
    new_events: int
    duplicate_events: int
    detail: str


class Health(BaseModel):
    status: str
    database: str
    event_count: int
