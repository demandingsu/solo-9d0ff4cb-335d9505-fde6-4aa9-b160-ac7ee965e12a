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
    root_batch: str = Field(
        ...,
        description=(
            "Batch of the assembly at the top of the first compatible path "
            "(the last INSTALL's child batch). See root_batches for all "
            "plausible batches when attribution is ambiguous."
        ),
    )
    root_batches: list[str] = Field(
        default_factory=list,
        description=(
            "Every parent-assembly batch that is compatible with the log for "
            "this terminal part. More than one entry means the board record "
            "did not identify which instance it went into."
        ),
    )
    is_finished_product: bool
    path: list[PathStep] = Field(
        ...,
        description="First compatible INSTALL/REMOVE event path (child -> root)",
    )
    compatible_paths: list[list[PathStep]] = Field(
        default_factory=list,
        description=(
            "All event paths compatible with the active log that end at this "
            "part. Convergent attributions keep every path for auditing."
        ),
    )


class TraceResponse(BaseModel):
    queried_at: str
    child_part_no: str
    child_batch: str
    batch_found: bool = Field(
        ...,
        description=(
            "False when the queried (part, batch) has no active INSTALL at "
            "the queried moment — it was never installed or was already "
            "removed. All result lists are empty in that case."
        ),
    )
    affected_products: list[AssemblyPath] = Field(
        ...,
        description=(
            "确定受影响: finished products (P-*) for which every "
            "log-compatible attribution path ends at the same product."
        ),
    )
    pending_products: list[AssemblyPath] = Field(
        ...,
        description=(
            "待核实: finished products (P-*) reached by at least one "
            "log-compatible path, but the parent-assembly batch cannot be "
            "determined uniquely (several possible parents or an in-progress "
            "chain). Each entry carries the corresponding event path."
        ),
    )
    open_chains: list[AssemblyPath] = Field(
        ...,
        description="Chains whose top assembly is not a finished product"
    )
    total_affected_products: int = Field(
        ..., description="数确定受影响成品 (distinct confirmed products)"
    )
    total_pending_products: int = Field(
        ..., description="待核实成品数 (distinct pending products)"
    )
    ambiguous: bool = Field(
        ...,
        description=(
            "True when the log-compatible attribution is not unique, i.e. "
            "more than one possible top chain exists. Convergent paths that "
            "all reach the same finished product are not ambiguous."
        ),
    )
    compatible_path_count: int = Field(
        ...,
        description="Number of event paths compatible with the active log at the queried moment",
    )


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
