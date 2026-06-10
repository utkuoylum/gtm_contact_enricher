from __future__ import annotations
import logging
import asyncio
import httpx
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from models import EnrichmentRequest, EnrichmentResult
from enricher import enrich
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
            ),
        )
        return result
    except Exception as e:
        logger.error(f"Enrichment failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}


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
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
