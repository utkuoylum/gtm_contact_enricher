from __future__ import annotations
import logging
import asyncio
import httpx
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from models import EnrichmentRequest, EnrichmentResult, PersonMatchRequest, PersonMatchResult
from enricher import enrich
from scrapers.apollo_scraper import match_person, apollo_available
from config import HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Contact Enrichment Webhook ready")
    yield
    executor.shutdown(wait=False)


app = FastAPI(
    title="Contact Enrichment Webhook",
    description="Finds hiring decision-makers at companies for job agency outreach",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/enrich", response_model=EnrichmentResult, summary="Enrich contacts for a company")
async def enrich_contacts(request: EnrichmentRequest, background_tasks: BackgroundTasks):
    """
    Synchronous enrichment (waits for result, up to ~90 seconds).
    If `callback_url` is provided, returns immediately and POSTs the result to that URL.
    """
    if request.callback_url:
        # Async mode: kick off in background, return 202 immediately
        background_tasks.add_task(_run_and_callback, request)
        return JSONResponse(
            status_code=202,
            content={
                "status": "processing",
                "message": f"Enrichment started. Result will be POSTed to {request.callback_url}",
                "company_name": request.company_name,
            },
        )

    # Sync mode: run and wait
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: enrich(
                request.company_name,
                request.location,
                request.job_category,
                request.max_contacts,
                request.find_direct_lines,
                request.domain or "",
            ),
        )
        return result
    except Exception as e:
        logger.error(f"Enrichment failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/match_person", response_model=PersonMatchResult, summary="Enrich one person via Apollo people/match")
async def match_single_person(request: PersonMatchRequest):
    """
    Enrich a single, already-identified person with email + phone.
    Identify by (name + company_name/domain), linkedin_url, or email.
    Costs 1 Apollo credit per successful match.
    """
    if not apollo_available():
        raise HTTPException(status_code=503, detail="APOLLO_API_KEY not configured")
    if not (request.linkedin_url or request.email or
            ((request.full_name or request.last_name) and (request.company_name or request.domain))):
        raise HTTPException(
            status_code=422,
            detail="Provide linkedin_url, email, or name + company_name/domain",
        )

    loop = asyncio.get_event_loop()
    try:
        m = await loop.run_in_executor(
            executor,
            lambda: match_person(
                full_name=request.full_name,
                first_name=request.first_name,
                last_name=request.last_name,
                company_name=request.company_name,
                domain=request.domain,
                linkedin_url=request.linkedin_url,
                email=request.email,
                reveal_personal_emails=request.reveal_personal_emails,
            ),
        )
    except Exception as e:
        logger.error(f"match_person failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    if not m:
        return PersonMatchResult(found=False)
    return PersonMatchResult(
        found=True,
        full_name=m.get("full_name"),
        title=m.get("title"),
        company=m.get("company"),
        email=m.get("email"),
        email_status=m.get("email_status"),
        phone=m.get("phone"),
        linkedin_url=m.get("linkedin_url"),
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/version")
async def version():
    """Return the deployed git commit SHA so deploys can be verified."""
    import os
    sha = (
        os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or os.environ.get("GIT_SHA")
        or "unknown"
    )
    return {"commit": sha, "short": sha[:7] if sha != "unknown" else "unknown"}


async def _run_and_callback(request: EnrichmentRequest):
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: enrich(
                request.company_name,
                request.location,
                request.job_category,
                request.max_contacts,
                request.find_direct_lines,
                request.domain or "",
            ),
        )
        payload = result.model_dump()
    except Exception as e:
        logger.error(f"Async enrichment failed: {e}", exc_info=True)
        payload = {
            "status": "error",
            "company_name": request.company_name,
            "error": str(e),
            "contacts": [],
        }

    # POST result back to callback URL
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(request.callback_url, json=payload)
        logger.info(f"Callback delivered to {request.callback_url}")
    except Exception as e:
        logger.error(f"Callback delivery failed to {request.callback_url}: {e}")


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", PORT))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, timeout_keep_alive=120)
