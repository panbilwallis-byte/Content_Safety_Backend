"""Source policy shared by listing, resolution and background workers."""
import os


def legacy_enabled():
    return os.getenv("AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC", "false").lower() in {"1", "true", "yes"}


def visible(batch):
    # Old generated value projections remain stored, but are opt-in only.
    return batch.source_system in {"upload", "internal_model", "internal_agent"} or legacy_enabled()


def origin(batch):
    return "platform" if batch.source_system in {"internal_model", "internal_agent"} else "external_import"
