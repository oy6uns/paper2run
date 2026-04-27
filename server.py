"""
PDF 수식 추출 API 서버
Run: uvicorn server:app --reload --port 8080
"""

import os
import uuid
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from openai import OpenAI
from supabase import create_client, Client

from extract_equations import (
    pdf_to_images,
    extract_equations_from_page,
    filter_core_equations,
    save_to_supabase,
)

# ── 설정 ──────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_SECRET = os.getenv("SUPABASE_SECRET_KEY")
# ─────────────────────────────────────────────────────

app = FastAPI(title="P4DS Equation Extractor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 프로덕션에서는 프론트 도메인으로 제한
    allow_methods=["*"],
    allow_headers=["*"],
)

# 처리 상태 인메모리 저장 (job_id → status)
# 프로덕션에서는 Supabase jobs 테이블로 교체
job_store: dict[str, dict] = {}


# ── 응답 스키마 ───────────────────────────────────────
class JobStatus(BaseModel):
    job_id: str
    status: Literal["processing", "done", "error"]
    cached: bool = False
    paper_id: str | None = None
    filename: str | None = None
    total_equations: int | None = None
    error: str | None = None


class EquationOut(BaseModel):
    id: str
    eq_number: int
    keep_rank: int | None
    role: str | None
    importance_hint: str | None
    latex: str
    description: str
    context: str
    core_reason: str | None
    section_hint: str | None
    page: int | None


class PaperOut(BaseModel):
    id: str
    filename: str
    title: str
    total_equations: int
    created_at: str


# ── 캐시 확인 헬퍼 ────────────────────────────────────
def find_cached_paper(sb: Client, filename: str) -> dict | None:
    """같은 파일명의 논문이 이미 DB에 있으면 반환, 없으면 None"""
    result = sb.table("papers").select("*").eq("filename", filename).execute()
    return result.data[0] if result.data else None


# ── 백그라운드 작업 ───────────────────────────────────
def run_extraction(job_id: str, tmp_path: str, filename: str):
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        sb = create_client(SUPABASE_URL, SUPABASE_SECRET)

        pdf_path = Path(tmp_path)
        pages = pdf_to_images(pdf_path)

        all_equations = []
        for page_num, img in pages:
            eqs = extract_equations_from_page(openai_client, page_num, img)
            all_equations.extend(eqs)

        all_equations = filter_core_equations(
            openai_client, pdf_path.stem, all_equations
        )
        paper_id = save_to_supabase(sb, filename, all_equations)

        job_store[job_id] = {
            "status": "done",
            "paper_id": paper_id,
            "filename": filename,
            "total_equations": len(all_equations),
            "cached": False,
        }

    except Exception as e:
        job_store[job_id] = {"status": "error", "error": str(e)}

    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── 엔드포인트 ────────────────────────────────────────
@app.post("/papers/extract", response_model=JobStatus, status_code=202)
async def extract_paper(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    force: bool = Query(False, description="True면 캐시 무시하고 재추출"),
):
    """
    PDF를 업로드하면 수식을 추출해 Supabase에 저장합니다.

    - 같은 파일명이 이미 DB에 있으면 즉시 캐시 결과를 반환합니다 (GPT 호출 없음).
    - force=true 를 붙이면 캐시를 무시하고 재추출합니다.
    - 처리는 백그라운드에서 실행되며, job_id로 완료 여부를 polling합니다.
    """
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF 파일만 업로드 가능합니다.")

    sb = create_client(SUPABASE_URL, SUPABASE_SECRET)

    # ── 캐시 히트 ─────────────────────────────────────
    if not force:
        cached = find_cached_paper(sb, file.filename)
        if cached:
            job_id = str(uuid.uuid4())
            job_store[job_id] = {
                "status": "done",
                "cached": True,
                "paper_id": cached["id"],
                "filename": cached["filename"],
                "total_equations": cached["total_equations"],
            }
            return JobStatus(job_id=job_id, cached=True, status="done", **{
                k: v for k, v in job_store[job_id].items()
                if k not in ("status", "cached")
            })

    # ── 캐시 미스: 백그라운드 추출 ──────────────────────
    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    job_id = str(uuid.uuid4())
    job_store[job_id] = {"status": "processing", "filename": file.filename, "cached": False}
    background_tasks.add_task(run_extraction, job_id, tmp_path, file.filename)

    return JobStatus(job_id=job_id, status="processing", filename=file.filename)


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job_status(job_id: str):
    """수식 추출 작업 상태 확인 (processing / done / error)"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return JobStatus(job_id=job_id, **job)


@app.get("/papers", response_model=list[PaperOut])
def list_papers():
    """저장된 논문 목록 조회"""
    sb = create_client(SUPABASE_URL, SUPABASE_SECRET)
    result = sb.table("papers").select("*").order("created_at", desc=True).execute()
    return result.data


@app.get("/papers/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: str):
    """논문 상세 조회"""
    sb = create_client(SUPABASE_URL, SUPABASE_SECRET)
    result = sb.table("papers").select("*").eq("id", paper_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="논문을 찾을 수 없습니다.")
    return result.data[0]


@app.get("/papers/{paper_id}/equations", response_model=list[EquationOut])
def get_equations(
    paper_id: str,
    role: str | None = Query(None, description="model / loss / update / inference / definition"),
):
    """논문의 핵심 수식 조회 (keep_rank 순, role 필터 선택)"""
    sb = create_client(SUPABASE_URL, SUPABASE_SECRET)
    query = (
        sb.table("equations")
        .select("*")
        .eq("paper_id", paper_id)
        .order("keep_rank", desc=False)
    )
    if role:
        query = query.eq("role", role)
    result = query.execute()
    return result.data


@app.get("/health")
def health():
    return {"status": "ok"}
