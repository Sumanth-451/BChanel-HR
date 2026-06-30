"""
POST /api/csv/upload  — receive Zoho Recruit CSV, screen all candidates via Claude,
write results to Google Sheets SCREEN tab, return summary + per-row results.
"""
import asyncio
from fastapi import APIRouter, File, UploadFile, HTTPException
from pydantic import BaseModel
from typing import Any, Optional, List, Dict

from tools.csv_parser import parse_zoho_csv
from tools.claude_screener import screen_candidate
from tools.sheets_writer import append_screened_candidate, update_human_approval, write_pre_rejected
from core.logging import get_logger

logger = get_logger("api.csv_workflow")
router = APIRouter(prefix="/csv", tags=["csv-workflow"])


class ScreeningSummary(BaseModel):
    total: int
    screened: int
    pre_rejected: int  # rejected by Zoho stage before Claude even runs
    failed: int
    strong_hire: int
    hire: int
    conditional: int
    do_not_advance: int
    results: List[Dict[str, Any]]       # SCREEN tab candidates (Strong Hire / Hire / Conditional)
    rejected_results: List[Dict[str, Any]]  # REJECTED tab candidates


class HumanApprovalRequest(BaseModel):
    row_number: int
    approved: bool
    result: Optional[Dict[str, Any]] = None  # candidate result — needed for rejection routing


@router.post("/upload", response_model=ScreeningSummary)
async def upload_and_screen(file: UploadFile = File(...)):
    """
    Accept a Zoho Recruit CSV export, screen every candidate via Claude API,
    append results to Google Sheets SCREEN tab, return summary.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files accepted.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Parse CSV
    try:
        candidates = parse_zoho_csv(content)
    except Exception as exc:
        logger.error("csv_parse_failed", error=str(exc))
        raise HTTPException(status_code=422, detail=f"CSV parse error: {exc}")

    if not candidates:
        raise HTTPException(status_code=422, detail="No candidate rows found in CSV.")

    # ── Pre-filter by Zoho Application Stage ──────────────────────────────
    # Stage = "Rejected" → write to REJECTED tab immediately, skip Claude
    loop = asyncio.get_event_loop()
    to_screen = []
    pre_rejected = []

    for c in candidates:
        if c.get("stage", "").strip().lower() == "rejected":
            pre_rejected.append(c)
        else:
            to_screen.append(c)

    # Write pre-rejected candidates to REJECTED tab (no Claude call needed)
    for c in pre_rejected:
        try:
            row_num = await loop.run_in_executor(None, write_pre_rejected, c)
            c["sheet_row"] = row_num
            c["recommendation"] = "Pre-Rejected (Zoho)"
            c["screen_result"] = "0"
            c["candidate_name"] = c.get("name", "")
        except Exception as exc:
            logger.error("pre_reject_write_failed", candidate=c.get("name"), error=str(exc))

    logger.info("pre_rejected_written", count=len(pre_rejected))

    # ── Screen remaining candidates via Claude ────────────────────────────
    semaphore = asyncio.Semaphore(5)
    results = []
    failed = 0

    async def _screen_one(candidate: dict) -> Optional[dict]:
        async with semaphore:
            try:
                result = await loop.run_in_executor(None, screen_candidate, candidate)
                row_num = await loop.run_in_executor(None, append_screened_candidate, result)
                result["sheet_row"] = row_num
                return result
            except Exception as exc:
                logger.error(
                    "candidate_screen_failed",
                    candidate=candidate.get("name"),
                    error=str(exc),
                )
                return None

    tasks = [_screen_one(c) for c in to_screen]
    raw_results = await asyncio.gather(*tasks)

    for r in raw_results:
        if r is None:
            failed += 1
        else:
            results.append(r)

    rec_counts = {
        "Strong Hire": 0,
        "Hire": 0,
        "Conditional": 0,
        "Do Not Advance": 0,
    }
    for r in results:
        key = r.get("recommendation", "Do Not Advance")
        if key in rec_counts:
            rec_counts[key] += 1

    # Split Claude results: Do Not Advance → REJECTED bucket
    screen_results   = [r for r in results if r.get("recommendation", "") != "Do Not Advance"]
    claude_rejected  = [r for r in results if r.get("recommendation", "") == "Do Not Advance"]
    all_rejected     = pre_rejected + claude_rejected

    logger.info(
        "batch_screen_complete",
        total=len(candidates),
        pre_rejected=len(pre_rejected),
        screened=len(screen_results),
        claude_rejected=len(claude_rejected),
        failed=failed,
    )

    return ScreeningSummary(
        total=len(candidates),
        screened=len(screen_results),
        pre_rejected=len(pre_rejected),
        failed=failed,
        strong_hire=rec_counts["Strong Hire"],
        hire=rec_counts["Hire"],
        conditional=rec_counts["Conditional"],
        do_not_advance=rec_counts["Do Not Advance"],
        results=screen_results,
        rejected_results=all_rejected,
    )


@router.post("/approve")
async def set_human_approval(req: HumanApprovalRequest):
    """
    Approve: set col Y = APPROVED on SCREEN tab.
    Reject:  mark SCREEN row as REJECTED + copy candidate to REJECTED tab.
    """
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, update_human_approval, req.row_number, req.approved, req.result
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sheets write failed: {exc}")

    return {"status": "ok", "row": req.row_number, "approved": req.approved}
