"""
PDF → Figure 추출기 (bbox 기반 크롭)
Pipeline:
  1. 페이지 이미지 → figure bbox 감지 → figure 크롭
  2. Supabase figures 저장

Usage: python extract_figures.py paper.pdf [paper2.pdf ...]
"""

import os, sys, json, base64, re
from pathlib import Path
from io import BytesIO

import fitz
from PIL import Image
from openai import OpenAI
from supabase import create_client, Client

# ── 설정 ──────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_SECRET = os.getenv("SUPABASE_SECRET_KEY")

MODEL          = "gpt-5.4-mini"
DPI            = 200
MAX_PAGES      = None
STORAGE_BUCKET = "figures"
# ─────────────────────────────────────────────────────

# ── Stage 1: 페이지에서 메인 파이프라인 figure 감지 ──
FIGURE_DETECT_PROMPT = """You are analyzing a page from a machine learning research paper.

Your task is to detect ONLY the single most likely main pipeline figure on this page.

A main pipeline figure is the paper's primary method overview, architecture diagram, framework diagram, or workflow figure.
It usually contains labeled modules, arrows, stages, or data/model flow.

Do NOT select:
- result plots
- ablation charts
- heatmaps
- tables
- qualitative examples
- ordinary text blocks
- body paragraphs below the caption

Return ONLY valid JSON:

{
  "has_main_pipeline_figure": true,
  "figure": {
    "figure_id": "fig1",
    "caption": "<full caption text, or empty string if not visible>",
    "figure_type": "<method_overview|architecture_diagram|flowchart|framework|other>",
    "figure_body_bbox": [x0, y0, x1, y1],
    "caption_bbox": [x0, y0, x1, y1],
    "crop_bbox": [x0, y0, x1, y1],
    "confidence": 0.0,
    "reason": "<brief reason why this is the main pipeline figure>",
    "key_insight": "<one sentence summary of what the figure shows>"
  }
}

Definitions:
- figure_body_bbox: tightly wraps ONLY the visual diagram, excluding caption.
- caption_bbox: tightly wraps ONLY the caption text belonging to the figure.
- crop_bbox: union of figure_body_bbox and caption_bbox, with a small margin.
- Do NOT include surrounding body paragraphs below the caption.
- Do NOT include text from neighboring columns.
- Coordinates must be normalized to the full page: [x0, y0, x1, y1].
- x goes left to right, y goes top to bottom.
- All values must be between 0.0 and 1.0.
- confidence must be between 0.0 and 1.0.

If no main pipeline figure is present on this page, return:

{
  "has_main_pipeline_figure": false,
  "figure": null
}

Do NOT include any text outside the JSON."""



# ── 유틸 ──────────────────────────────────────────────
def pdf_to_images(pdf_path: Path) -> list[tuple[int, Image.Image]]:
    doc = fitz.open(str(pdf_path))
    images = []
    pages = range(len(doc)) if MAX_PAGES is None else range(min(MAX_PAGES, len(doc)))
    for i in pages:
        page = doc[i]
        mat = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append((i + 1, img))
    doc.close()
    return images


def to_b64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def expand_bbox(bbox: list[float], pad_x: float = 0.015, pad_y: float = 0.01) -> list[float]:
    """bbox를 비율 단위로 확장. y1은 본문 침범 방지를 위해 pad_y만큼만."""
    x0, y0, x1, y1 = bbox
    return [
        max(0.0, x0 - pad_x),
        max(0.0, y0 - pad_y),
        min(1.0, x1 + pad_x),
        min(1.0, y1 + pad_y),
    ]


def crop_bbox(img: Image.Image, bbox: list[float]) -> Image.Image:
    """bbox [x0,y0,x1,y1] (0-1 fractions) → PIL crop."""
    w, h = img.size
    x0 = max(0, int(bbox[0] * w))
    y0 = max(0, int(bbox[1] * h))
    x1 = min(w, int(bbox[2] * w))
    y1 = min(h, int(bbox[3] * h))
    if x1 - x0 < 20 or y1 - y0 < 20:
        return img
    return img.crop((x0, y0, x1, y1))


def upload_figure_to_storage(sb: Client, img: Image.Image, paper_stem: str, fig_id: str) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    path = f"{paper_stem}/{fig_id}.jpg"
    sb.storage.from_(STORAGE_BUCKET).upload(
        path=path,
        file=buf.getvalue(),
        file_options={"content-type": "image/jpeg", "upsert": "true"},
    )
    return sb.storage.from_(STORAGE_BUCKET).get_public_url(path)


def call_vision(client: OpenAI, system: str, img: Image.Image, user_text: str) -> dict:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{to_b64(img)}", "detail": "high"}},
                {"type": "text", "text": user_text},
            ]},
        ],
        max_completion_tokens=3000,
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(m.group()) if m else {}


# ── Stage 1 ───────────────────────────────────────────
def detect_figures_on_page(client: OpenAI, page_num: int, page_img: Image.Image) -> list[dict]:
    try:
        data = call_vision(client, FIGURE_DETECT_PROMPT, page_img,
                           f"Detect the main pipeline figure on page {page_num}.")
        if not data.get("has_main_pipeline_figure"):
            return []
        fig = data.get("figure")
        if not fig:
            return []
        fig["page"] = page_num
        # 각 bbox 유효성 검사 및 기본값
        for key in ("figure_body_bbox", "caption_bbox", "crop_bbox"):
            bb = fig.get(key, [])
            if len(bb) != 4 or not all(0.0 <= v <= 1.0 for v in bb):
                fig[key] = [0.0, 0.0, 1.0, 1.0]
        # crop_bbox에 안전 패딩 적용
        fig["crop_bbox"] = expand_bbox(fig["crop_bbox"], pad_x=0.015, pad_y=0.01)
        # page_bbox는 crop_bbox로 통일 (DB 저장용)
        fig["page_bbox"] = fig["crop_bbox"]
        return [fig]
    except Exception as e:
        print(f"  [!] Page {page_num} figure detect error: {e}")
        return []



# ── Supabase 저장 ─────────────────────────────────────
def get_or_create_paper_id(sb: Client, filename: str) -> str:
    result = sb.table("papers").select("id").eq("filename", filename).execute()
    if result.data:
        return result.data[0]["id"]
    return sb.table("papers").insert({
        "filename": filename,
        "title": Path(filename).stem.replace("_", " "),
        "total_equations": 0,
    }).execute().data[0]["id"]


def save_to_supabase(sb: Client, paper_id: str, figures: list[dict]) -> dict[str, str]:
    """figures 저장. 반환: {figure_id → db UUID}"""
    sb.table("figures").delete().eq("paper_id", paper_id).execute()

    fig_id_map: dict[str, str] = {}
    if not figures:
        return fig_id_map

    for i, fig in enumerate(figures):
        row = {
            "paper_id": paper_id,
            "fig_number": i + 1,
            "figure_id": fig.get("figure_id", f"fig{i+1}"),
            "page": fig.get("page"),
            "caption": fig.get("caption", ""),
            "figure_type": fig.get("figure_type", "other"),
            "page_bbox": fig.get("page_bbox", []),
            "image_url": fig.get("image_url", ""),
            "layout": fig.get("reason", ""),
            "key_insight": fig.get("key_insight", ""),
        }
        db_fig = sb.table("figures").insert(row).execute().data[0]
        fig_id_map[fig.get("figure_id", f"fig{i+1}")] = db_fig["id"]

    return fig_id_map


# ── 메인 파이프라인 ────────────────────────────────────
def process_pdf(pdf_path: Path, client: OpenAI, sb: Client) -> None:
    print(f"\n[{pdf_path.name}] 처리 시작")
    pages = pdf_to_images(pdf_path)
    print(f"  총 {len(pages)}페이지")

    paper_id = get_or_create_paper_id(sb, pdf_path.name)
    paper_stem = pdf_path.stem
    all_figures = []

    for page_num, page_img in pages:
        print(f"  페이지 {page_num}/{len(pages)} — figure 감지 중...", end="\r")
        figures = detect_figures_on_page(client, page_num, page_img)
        if not figures:
            continue

        for fig in figures:
            raw_fid = fig.get("figure_id", "fig")
            fig_id = f"p{page_num}_{raw_fid}"
            fig["figure_id"] = fig_id

            fig_img = crop_bbox(page_img, fig["crop_bbox"])
            fig["image_url"] = upload_figure_to_storage(sb, fig_img, paper_stem, fig_id)

            print(f"  [{fig_id}] Storage 업로드 완료: {fig['image_url']}")
            all_figures.append(fig)

    print(f"\n  → 총 {len(all_figures)}개 figure, Supabase 저장 중...")

    # JSON 백업
    out_dir = Path(__file__).parent / "figures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{paper_stem}.json").write_text(
        json.dumps({"source": pdf_path.name, "paper_id": paper_id,
                    "total_figures": len(all_figures), "figures": all_figures},
                   ensure_ascii=False, indent=2)
    )

    try:
        save_to_supabase(sb, paper_id, all_figures)
        print(f"  Supabase 저장 완료 (paper_id: {paper_id})")
    except Exception as e:
        print(f"  [!] Supabase 저장 실패: {e}")
        print(f"  → schema.sql 의 migration SQL을 Supabase에서 실행하세요.")


def main():
    missing = [k for k, v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SECRET_KEY": SUPABASE_SECRET,
    }.items() if not v]
    if missing:
        print(f"오류: 환경변수 미설정 → {', '.join(missing)}")
        sys.exit(1)

    client = OpenAI(api_key=OPENAI_API_KEY)
    sb = create_client(SUPABASE_URL, SUPABASE_SECRET)

    pdf_paths = [Path(p) for p in sys.argv[1:]] if len(sys.argv) > 1 \
        else sorted(Path(__file__).parent.glob("*.pdf"))

    if not pdf_paths:
        print("처리할 PDF가 없습니다.")
        sys.exit(0)

    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            print(f"[!] 파일 없음: {pdf_path}")
            continue
        process_pdf(pdf_path, client, sb)

    print("\n모든 파일 처리 완료.")


if __name__ == "__main__":
    main()
