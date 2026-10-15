"""Name-compatible view of `steward.excerpts` for Plan 4 (`evidence_excerpts.MAX_EXCERPT_CHARS`, `MAX_EXCERPTS`,
`MAX_REQUEST_CHARS`, `select`, `build_request`). One implementation; this module only re-exports it."""
from steward.excerpts import (CLAIM_MAX_CHARS, EXCERPT_MAX_CHARS, LABELS, MAX_EXCERPT_CHARS, MAX_EXCERPTS,  # noqa: F401
                              MAX_REQUEST_CHARS, REQUEST_MAX_BYTES, Excerpt, build_request, finalize_label, is_stale,
                              parse_answer, record_snapshot, select)
