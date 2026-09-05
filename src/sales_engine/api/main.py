"""Main FastAPI application."""

import os
import shutil
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.sales_engine.core.orchestrator import SalesEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Sales Engine",
    description="AI-Powered Digital Product Sales Engine",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global engine instance
engine = SalesEngine()

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


class UploadResponse(BaseModel):
    success: bool
    product_name: str
    description: str
    keywords: list[str]
    search_queries: list[str]
    platform_descriptions: dict
    file_name: str
    file_type: str


class DistributeRequest(BaseModel):
    product_link: Optional[str] = None
    price: float = 0.0
    platforms: list[str] = ["gumroad", "lemonsqueezy", "alternativeto", "indiehackers"]


class DistributeResponse(BaseModel):
    success: bool
    results: list[dict]


class FindBuyersRequest(BaseModel):
    product_link: str
    max_leads: int = 20
    auto_send: bool = True


class FindBuyersResponse(BaseModel):
    success: bool
    results: list[dict]


class FullPipelineRequest(BaseModel):
    price: float = 0.0


@app.get("/")
async def root():
    return {
        "name": "Sales Engine",
        "version": "0.1.0",
        "status": "running",
        "endpoints": [
            "POST /upload - Upload a product",
            "POST /distribute - Distribute to platforms",
            "POST /find-buyers - Find and contact buyers",
            "POST /pipeline - Run full pipeline",
        ],
    }


@app.post("/upload", response_model=UploadResponse)
async def upload_product(file: UploadFile = File(...)):
    """Upload a product file for analysis."""
    try:
        file_path = UPLOAD_DIR / file.filename
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Determine file type
        file_extension = file.filename.split(".")[-1].lower()
        file_type_map = {
            "pdf": "PDF",
            "zip": "ZIP Archive",
            "notion": "Notion Template",
            "docx": "Word Document",
            "xlsx": "Excel Spreadsheet",
            "txt": "Text File",
            "json": "JSON",
            "py": "Python Script",
            "js": "JavaScript",
            "html": "HTML Template",
            "figma": "Figma Design",
            "sketch": "Sketch Design",
        }
        file_type = file_type_map.get(file_extension, file.content_type or "Unknown")

        # Analyze
        analysis = await engine.upload_and_analyze(str(file_path), file_type)
        if not analysis.product_info:
            raise HTTPException(status_code=500, detail="Failed to analyze product")

        return UploadResponse(
            success=True,
            product_name=analysis.product_info.name,
            description=analysis.product_info.description,
            keywords=analysis.product_info.keywords,
            search_queries=analysis.product_info.search_queries,
            platform_descriptions=analysis.product_info.platform_descriptions,
            file_name=file.filename,
            file_type=file_type,
        )
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/distribute", response_model=DistributeResponse)
async def distribute_product(request: DistributeRequest):
    """Distribute the current product to platforms."""
    try:
        results = await engine.distribute_to_all_platforms(price=request.price)
        return DistributeResponse(
            success=True,
            results=[
                {
                    "platform": r.platform,
                    "success": r.success,
                    "message": r.message,
                    "url": r.url,
                }
                for r in results
            ],
        )
    except Exception as e:
        logger.error(f"Distribution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/find-buyers", response_model=FindBuyersResponse)
async def find_buyers(request: FindBuyersRequest):
    """Find buyers and contact them."""
    try:
        results = await engine.find_and_contact_buyers(
            max_leads=request.max_leads,
            auto_send=request.auto_send,
        )
        return FindBuyersResponse(
            success=True,
            results=[
                {
                    "platform": r.platform,
                    "leads_found": r.leads_found,
                    "high_intent_leads": r.high_intent_leads,
                    "messages_sent": r.messages_sent,
                }
                for r in results
            ],
        )
    except Exception as e:
        logger.error(f"Find buyers failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pipeline")
async def run_pipeline(file: UploadFile = File(...), price: float = Form(0.0)):
    """Run the full pipeline: upload + analyze + distribute + find buyers."""
    try:
        # Save file
        file_path = UPLOAD_DIR / file.filename
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Determine file type
        file_extension = file.filename.split(".")[-1].lower()
        file_type = f"File ({file_extension})"

        # Run full pipeline
        result = await engine.run_full_pipeline(
            file_path=str(file_path),
            file_type=file_type,
            price=price,
        )
        return result
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Simple HTML dashboard."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Sales Engine Dashboard</title>
        <style>
            body { font-family: system-ui; max-width: 1200px; margin: 40px auto; padding: 0 20px; }
            h1 { color: #2563eb; }
            .card { background: #f3f4f6; border-radius: 8px; padding: 20px; margin: 20px 0; }
            .metric { display: inline-block; margin: 0 20px; }
            .metric .value { font-size: 32px; font-weight: bold; color: #2563eb; }
            .metric .label { color: #6b7280; font-size: 14px; }
        </style>
    </head>
    <body>
        <h1>Sales Engine Dashboard</h1>
        <p>AI-Powered Digital Product Sales Engine</p>

        <div class="card">
            <h2>Quick Actions</h2>
            <p>POST /pipeline - Upload a product and run the full pipeline</p>
            <p>POST /upload - Just analyze a product</p>
            <p>POST /distribute - Distribute to platforms</p>
            <p>POST /find-buyers - Find and contact buyers</p>
        </div>

        <div class="card">
            <h2>How It Works</h2>
            <ol>
                <li>Upload a product file (PDF, ZIP, etc.)</li>
                <li>AI analyzes and generates product info</li>
                <li>Auto-distributes to Gumroad, Lemon Squeezy, AlternativeTo, IndieHackers</li>
                <li>AI monitors Reddit + Twitter for buyers</li>
                <li>AI generates and sends personalized replies</li>
            </ol>
        </div>

        <div class="card">
            <h2>Channels</h2>
            <div class="metric">
                <div class="value">5+</div>
                <div class="label">Distribution Platforms</div>
            </div>
            <div class="metric">
                <div class="value">2</div>
                <div class="label">Buyer Finder Sources</div>
            </div>
            <div class="metric">
                <div class="value">$0</div>
                <div class="label">Cost Until First Sale</div>
            </div>
        </div>
    </body>
    </html>
    """
    return html


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)