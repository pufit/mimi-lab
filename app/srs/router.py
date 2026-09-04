"""SRS HTTP routes — every §7.2 endpoint, mounted at `/api/srs`.

The router is a thin delegation layer: it validates the request models of
`app/models.py` and turns the service's error contract into HTTP.

  NotImplementedError -> 501   (hour-0 skeleton: the shape is frozen, the body
                                lands with WP-A/WP-B)
  SrsNotFound         -> 404
  SrsConflict         -> 409   (`payload` is returned verbatim as the body, so
                                `SrsReviewConflict` = `{detail, card}`)
  SrsInvalid          -> 422
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..models import (
    SrsActionRequest,
    SrsBulkRequest,
    SrsBulkResult,
    SrsCandidate,
    SrsCandidates,
    SrsCard,
    SrsCardDetail,
    SrsCardList,
    SrsCreateCard,
    SrsGenerateRequest,
    SrsGeneration,
    SrsImportReport,
    SrsImportRequest,
    SrsMove,
    SrsPatchCard,
    SrsQueue,
    SrsResort,
    SrsReviewRequest,
    SrsReviewResult,
    SrsSettings,
    SrsSettingsPatch,
    SrsStats,
    SrsSummary,
    SrsSwapMoment,
    SrsUndoRequest,
    SrsUndoResult,
)
from . import service

log = logging.getLogger("mimi_lab.srs.router")

router = APIRouter(prefix="/srs", tags=["srs"])


def _api(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Map the service error contract onto HTTP status codes."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=f"srs: not implemented yet ({e})")
        except service.SrsNotFound as e:
            raise HTTPException(status_code=404, detail=str(e) or "not found")
        except service.SrsConflict as e:
            if e.payload is not None:
                return JSONResponse(status_code=409, content=jsonable_encoder(e.payload))
            raise HTTPException(status_code=409, detail=e.detail)
        except service.SrsInvalid as e:
            raise HTTPException(status_code=422, detail=str(e) or "invalid request")

    return wrapper


# --- deck & review ---------------------------------------------------------

@router.get("/summary", response_model=SrsSummary)
@_api
def summary():
    """Deck header + nav badge (polled every 60 s)."""
    return service.summary()


@router.get("/queue", response_model=SrsQueue)
@_api
def queue(limit: int = Query(default=20, ge=1, le=100), extra_new: int = Query(default=0, ge=0, le=50)):
    """The review queue (§3.6). `extra_new` bypasses `new_per_day` once."""
    return service.queue(limit=limit, extra_new=extra_new)


@router.post("/review", response_model=SrsReviewResult)
@_api
def review(req: SrsReviewRequest):
    """Rate one card. **200** result · **404** unknown card · **409**
    `SrsReviewConflict` (the card is no longer active; the body carries it) ·
    **422** bad rating. The same `client_id` twice replays the stored result
    with `duplicate: true` and writes nothing (§3.10)."""
    return service.review(req)


@router.post("/review/undo", response_model=SrsUndoResult)
@_api
def review_undo(req: SrsUndoRequest = Body(default=SrsUndoRequest())):
    """Undo the newest review of today, or `review_id` (§3.11).

    **200** `SrsUndoResult` · **404** nothing undoable today (or that row is
    already undone) · **409** the card has a newer non-undone review."""
    return service.undo_review(req.review_id)


# --- cards -----------------------------------------------------------------

@router.get("/cards", response_model=SrsCardList)
@_api
def list_cards(
    state: str = Query(default="all"),
    q: str = Query(default=""),
    sort: str = Query(default="queue"),
    order: str = Query(default="asc"),
    limit: int = Query(default=50, ge=0),          # limit=0 = no limit (whole stack)
    offset: int = Query(default=0, ge=0),
):
    return service.list_cards(state=state, q=q, sort=sort, order=order, limit=limit, offset=offset)


@router.post("/cards/bulk", response_model=SrsBulkResult)
@_api
def bulk(req: SrsBulkRequest):
    """One transaction; `previous` enables the client's exact-inverse undo."""
    return service.bulk_action(req)


@router.post("/cards")
@_api
def create_card(req: SrsCreateCard):
    """**201** `{card, warning}` with a `line_id` · **202** `{queued, lemma}`
    without one (a judge job looks for a moment) · **404** unknown line ·
    **409** `{detail, card_id}` when the lemma already has a card · **422** the
    word does not occur in that line (§5.12)."""
    out = service.create_card_manual(req)
    status = 202 if out.get("queued") else 201
    return JSONResponse(status_code=status, content=jsonable_encoder(out))


@router.get("/cards/{card_id}", response_model=SrsCardDetail)
@_api
def card_detail(card_id: int):
    return service.card_detail(card_id)


@router.patch("/cards/{card_id}")
@_api
def patch_card(card_id: int, req: SrsPatchCard):
    return {"card": service.patch_card(card_id, req)}


@router.delete("/cards/{card_id}", status_code=204)  # 204 no content · 404 unknown card
@_api
def delete_card(card_id: int):
    service.delete_card(card_id)
    return Response(status_code=204)


@router.post("/cards/{card_id}/action")
@_api
def card_action(card_id: int, req: SrsActionRequest):
    """**200** `{card}` · **404** unknown card · **422** an action that is not
    allowed from the current state or on a confirm-known card (§4.2)."""
    return {"card": service.card_action(card_id, req.action)}


@router.post("/cards/{card_id}/moment")
@_api
def swap_moment(card_id: int, req: SrsSwapMoment):
    """Swap the primary moment (exactly one of `moment_id` / `line_id`).

    **200** `{card}` · **404** unknown card/line/moment · **409** the lemma is
    not in that line (or the moment lost its line) · **422** neither or both
    ids given, or a confirm-known card (§4.2)."""
    return {"card": service.set_moment(card_id, req)}


@router.post("/cards/{card_id}/clip")
@_api
def regenerate_clip(card_id: int):
    """**200** `{card, queued}` · **404** unknown card · **409** the episode
    video is not on disk (§4.2)."""
    return {"card": service.regenerate_clip(card_id), "queued": True}


@router.post("/cards/{card_id}/find-moments", status_code=202)
@_api
def find_moments(card_id: int):
    return service.find_moments(card_id)


# --- stack -----------------------------------------------------------------

@router.post("/stack/move")
@_api
def stack_move(req: SrsMove):
    """The only reorder primitive: one card, absolute 0-based position."""
    return {"card": service.stack_move(req)}


@router.post("/stack/resort")
@_api
def stack_resort(req: SrsResort):
    return service.stack_resort(req)


# --- generation & candidates ------------------------------------------------

@router.post("/generate", status_code=202)
@_api
def generate(req: SrsGenerateRequest = Body(default=SrsGenerateRequest())):
    """**202** `{queued, run_id}` · **409** only when a run is open WITH a live
    `srs_judge_batch` job (a run whose batch died is swept first, §5.9)."""
    return service.start_generation(req)


@router.get("/generation", response_model=SrsGeneration)
@_api
def generation(limit: int = Query(default=10, ge=1, le=50)):
    return service.generation(limit=limit)


@router.get("/candidates", response_model=SrsCandidates)
@_api
def candidates(
    status: str = Query(default="unjudged"),
    q: str = Query(default=""),
    sort: str = Query(default="score"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    refresh: int = Query(default=0, ge=0, le=1),
):
    return service.candidates(
        status=status, q=q, sort=sort, limit=limit, offset=offset, refresh=bool(refresh)
    )


@router.post("/words/{lemma}/skip")
@_api
def word_skip(lemma: str):
    return {"candidate": service.word_skip(lemma)}


@router.post("/words/{lemma}/unskip")
@_api
def word_unskip(lemma: str):
    return {"candidate": service.word_unskip(lemma)}


@router.post("/words/{lemma}/confirm-known")
@_api
def word_confirm_known(lemma: str):
    return {"candidate": service.word_confirm_known(lemma)}


@router.post("/words/{lemma}/judge", status_code=202)
@_api
def word_judge(lemma: str):
    return service.word_judge(lemma)


# --- import, settings, stats ------------------------------------------------

@router.post("/import", response_model=SrsImportReport)
@_api
def import_deck(req: SrsImportRequest = Body(default=SrsImportRequest())):
    """The curated initial deck (§5.11). Deliberately NOT behind `require_token`:
    the SPA sends no bearer token, so a token-guarded route would always 401 from
    the Deck button. `_origin_guard` protects it like every other write, and
    `path` must resolve under `ROOT/data` (422 otherwise)."""
    return service.import_deck(req.path)


@router.get("/settings", response_model=SrsSettings)
@_api
def get_settings():
    return service.settings_get()


@router.put("/settings", response_model=SrsSettings)
@_api
def put_settings(req: SrsSettingsPatch):
    """Partial update; out-of-range values are rejected by the model (422)."""
    return service.settings_put(req)


@router.get("/stats", response_model=SrsStats)
@_api
def stats(days: int = Query(default=90, ge=1, le=730)):
    return service.stats(days=days)


# Response models referenced by the OpenAPI schema of the dict-returning routes
# (`{"card": …}`, `{"candidate": …}`) — kept imported so the contract is visible
# to the frontend generator and so unused-import linting stays honest.
_CONTRACT_MODELS = (SrsCard, SrsCandidate)
