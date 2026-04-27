"""
PDF → 핵심 수식 추출기 + Supabase 저장
Usage: python extract_equations.py paper.pdf [paper2.pdf ...]
       python extract_equations.py  (폴더 내 모든 PDF 자동 처리)
"""

import os
import sys
import json
import base64
import re
from pathlib import Path
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image
from openai import OpenAI
from supabase import create_client, Client

# ── 설정 ──────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_SECRET   = os.getenv("SUPABASE_SECRET_KEY")

MODEL     = "gpt-5.4-mini"
DPI       = 150    # 페이지 렌더링 해상도
MAX_PAGES = None   # None = 전체, 숫자 지정 시 앞 N페이지만
# ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a mathematical equation extractor for academic papers.
Given an image of a paper page, extract ALL equations that MIGHT be important to the paper's method.
Be generous — it is better to include a borderline equation than to miss a core one.
A second-stage filter will remove the non-essential ones later.

EXTRACT if the equation:
- Looks like a model definition, objective, loss, update rule, or inference procedure
- Is numbered or displayed (block equation), not just inline notation
- Appears in sections like Method, Model, Approach, Training, Inference, or Algorithm
- Could be needed by someone trying to reimplement the paper

SKIP only if the equation is clearly:
- Inside a proof, theorem, or lemma block
- A pure figure caption with no standalone math
- From the References or Appendix notation list
- A trivial scalar assignment (e.g., d = 512, T = 1000)

Extract up to 6 equations per page. If nothing qualifies, return an empty list.

Return ONLY valid JSON:
{
  "equations": [
    {
      "latex": "<complete, self-contained LaTeX string>",
      "description": "<one sentence: what this equation represents>",
      "context": "<1-2 sentences from the paper surrounding this equation>",
      "section_hint": "<section name if visible, e.g. 'Method', 'Training', 'Inference', or ''>"
    }
  ]
}

If no equations qualify on this page, return: {"equations": []}
Do NOT include any text outside the JSON."""


def pdf_to_images(pdf_path: Path, dpi: int = DPI) -> list[tuple[int, Image.Image]]:
    doc = fitz.open(str(pdf_path))
    images = []
    pages = range(len(doc)) if MAX_PAGES is None else range(min(MAX_PAGES, len(doc)))
    for i in pages:
        page = doc[i]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append((i + 1, img))
    doc.close()
    return images


def image_to_base64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def extract_equations_from_page(
    openai_client: OpenAI, page_num: int, img: Image.Image
) -> list[dict]:
    b64 = image_to_base64(img)
    try:
        response = openai_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}",
                                "detail": "high",
                            },
                        },
                        {
                            "type": "text",
                            "text": f"Extract all important equations from page {page_num}.",
                        },
                    ],
                },
            ],
            max_completion_tokens=2000,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return []
        data = json.loads(json_match.group())
        equations = data.get("equations", [])
        valid = []
        for eq in equations:
            latex = eq.get("latex", "")
            if latex and not latex.lower().startswith("\\text{no"):
                eq["page"] = page_num
                valid.append(eq)
        return valid

    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [!] Page {page_num} parse error: {e}")
        return []
    except Exception as e:
        print(f"  [!] Page {page_num} API error: {e}")
        return []


FILTER_PROMPT = """You are an expert reviewer of machine learning papers.

Below is a list of equation candidates extracted from the paper "{title}".
Your task is to select ONLY the equations that are essential to understanding or reimplementing the paper's proposed method.

KEEP equations that:
- Define the central proposed model, architecture, module, scoring function, or algorithm
- Define the main loss, objective, training procedure, update rule, or inference/sampling rule
- Are explicitly tied to the paper's claimed contribution
- Would be needed by a reader to reproduce the method

REMOVE equations that are:
- Proofs, theorem steps, lemmas, corollaries, convergence bounds, or intermediate derivations
- Standard background formulas copied from prior work
- Simple notation definitions or tensor shape definitions
- Standalone hyperparameter values
- Experimental metrics, ablation formulas, or analysis-only equations
- Duplicates or near-duplicates of already selected equations
- Equations whose role is unclear and not necessary for method implementation

Selection priority:
1. Proposed method equations
2. Main training objective/loss
3. Inference or update equations
4. Method-specific definitions required to understand the above

Final count:
- Prefer 3–8 equations.
- Fewer is acceptable if the paper has fewer true core method equations.
- Do not keep weak equations just to reach a target count.

For each selected equation, preserve ALL original fields (latex, description, context, section_hint).
Also ADD these new fields:
- "role": one of ["model", "loss", "update", "inference", "definition"]
- "importance_hint": one of ["high", "medium"]
- "core_reason": one sentence explaining why this equation is core to the method
- "keep_rank": integer starting from 1 (1 = most central to the paper)

Input equations:
{equations_json}

Return ONLY valid JSON:
{{"equations": [...]}}
Do NOT include any text outside the JSON."""


def _latex_signature(latex: str) -> str:
    """Strip whitespace/braces variations for rough dedup comparison."""
    return re.sub(r'[\s{}]', '', latex).lower()


def dedup_equations(equations: list[dict]) -> list[dict]:
    """Remove near-identical LaTeX strings, keeping the first occurrence."""
    seen: set[str] = set()
    result = []
    for eq in equations:
        sig = _latex_signature(eq.get("latex", ""))
        if sig and sig not in seen:
            seen.add(sig)
            result.append(eq)
    return result


def filter_core_equations(client: OpenAI, title: str, equations: list[dict]) -> list[dict]:
    # Code-level dedup before sending to LLM
    equations = dedup_equations(equations)
    if not equations:
        return []
    prompt = FILTER_PROMPT.format(
        title=title,
        equations_json=json.dumps(equations, ensure_ascii=False, indent=2),
    )
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4000,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return equations
        data = json.loads(json_match.group())
        # Final code-level dedup in case LLM still returns duplicates
        return dedup_equations(data.get("equations", equations))
    except Exception as e:
        print(f"  [!] Filter step error: {e}")
        return equations


def save_to_supabase(
    sb: Client, filename: str, all_equations: list[dict]
) -> str:
    # 같은 파일명이면 기존 데이터 덮어쓰기 (upsert)
    paper_res = sb.table("papers").upsert(
        {
            "filename": filename,
            "title": Path(filename).stem.replace("_", " "),
            "total_equations": len(all_equations),
        },
        on_conflict="filename",
    ).execute()

    paper_id = paper_res.data[0]["id"]

    # 기존 수식 삭제 후 재삽입
    sb.table("equations").delete().eq("paper_id", paper_id).execute()

    if all_equations:
        rows = [
            {
                "paper_id": paper_id,
                "eq_number": i + 1,
                "page": eq.get("page"),
                "latex": eq.get("latex", ""),
                "description": eq.get("description", ""),
                "context": eq.get("context", ""),
                "section_hint": eq.get("section_hint", ""),
                "role": eq.get("role", ""),
                "importance_hint": eq.get("importance_hint", ""),
                "core_reason": eq.get("core_reason", ""),
                "keep_rank": eq.get("keep_rank"),
            }
            for i, eq in enumerate(all_equations)
        ]
        sb.table("equations").insert(rows).execute()

    return paper_id


def process_pdf(
    pdf_path: Path,
    openai_client: OpenAI,
    sb: Client,
    output_dir: Path,
) -> None:
    print(f"\n[{pdf_path.name}] 처리 시작")
    pages = pdf_to_images(pdf_path)
    print(f"  총 {len(pages)}페이지")

    all_equations = []
    for page_num, img in pages:
        print(f"  페이지 {page_num}/{len(pages)} 분석 중...", end="\r")
        eqs = extract_equations_from_page(openai_client, page_num, img)
        all_equations.extend(eqs)

    print(f"\n  {len(all_equations)}개 후보 → 핵심 수식 필터링 중...")
    all_equations = filter_core_equations(openai_client, pdf_path.stem, all_equations)
    print(f"  → 최종 {len(all_equations)}개 수식 → Supabase 저장 중...")
    paper_id = save_to_supabase(sb, pdf_path.name, all_equations)
    print(f"  저장 완료 (paper_id: {paper_id})")

    # JSON 백업도 유지
    out_path = output_dir / f"{pdf_path.stem}.json"
    out_path.write_text(
        json.dumps(
            {
                "source": pdf_path.name,
                "paper_id": paper_id,
                "total_equations": len(all_equations),
                "equations": all_equations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"  JSON 백업: {out_path}")


def main():
    missing = [k for k, v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SECRET_KEY": SUPABASE_SECRET,
    }.items() if not v]
    if missing:
        print(f"오류: 환경변수 미설정 → {', '.join(missing)}")
        print("  export $(cat .env | xargs)")
        sys.exit(1)

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    sb = create_client(SUPABASE_URL, SUPABASE_SECRET)

    base_dir = Path(__file__).parent
    output_dir = base_dir / "equations"
    output_dir.mkdir(exist_ok=True)

    pdf_paths = [Path(p) for p in sys.argv[1:]] if len(sys.argv) > 1 \
        else sorted(base_dir.glob("*.pdf"))

    if not pdf_paths:
        print("처리할 PDF 파일이 없습니다.")
        print("  P4DS/ 폴더에 PDF를 넣거나: python extract_equations.py paper.pdf")
        sys.exit(0)

    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            print(f"[!] 파일 없음: {pdf_path}")
            continue
        process_pdf(pdf_path, openai_client, sb, output_dir)

    print("\n모든 파일 처리 완료.")


if __name__ == "__main__":
    main()
