# Paper2Run API

PDF 논문에서 핵심 수식과 figure를 자동으로 추출하는 API 서버입니다.

**Base URL:** `https://paper2run-production.up.railway.app`

---

## Endpoints 전체 목록

| Method | Path | 설명 |
|--------|------|------|
| `POST` | `/papers/extract` | PDF 업로드 → 수식 추출 시작 |
| `POST` | `/papers/figures/extract` | PDF 업로드 → figure 추출 시작 |
| `GET` | `/jobs/{job_id}` | 작업 상태 확인 (polling) |
| `GET` | `/papers` | 저장된 논문 목록 |
| `GET` | `/papers/{paper_id}` | 논문 상세 |
| `GET` | `/papers/{paper_id}/equations` | 논문의 핵심 수식 목록 |
| `GET` | `/papers/{paper_id}/figures` | 논문의 figure 목록 (이미지 URL 포함) |
| `GET` | `/health` | 서버 상태 확인 |

---

## 수식 추출 Flow

```
POST /papers/extract
  → GET /jobs/{job_id}          (status: "done" 될 때까지 polling)
  → GET /papers/{paper_id}/equations
```

같은 파일명의 논문이 이미 DB에 있으면 GPT 호출 없이 즉시 반환됩니다 (`cached: true`).

## Figure 추출 Flow

```
POST /papers/figures/extract
  → GET /jobs/{job_id}          (status: "done" 될 때까지 polling)
  → GET /papers/{paper_id}/figures   (image_url로 이미지 접근)
```

---

## POST `/papers/extract`

PDF를 업로드하면 수식 추출을 시작합니다.

**Request** `multipart/form-data`
| Field | Type | Description |
|-------|------|-------------|
| `file` | File | PDF 파일 |

**Query Params**
| Param | Default | Description |
|-------|---------|-------------|
| `force` | `false` | `true`로 설정하면 캐시 무시하고 재추출 |

**Response**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "processing",
  "cached": false,
  "filename": "attention_is_all_you_need.pdf"
}
```

캐시 히트 시 `status: "done"` 과 `paper_id` 가 즉시 반환됩니다.

---

## POST `/papers/figures/extract`

PDF를 업로드하면 figure 추출을 시작합니다.  
figure bbox 감지 → 크롭 저장 → Supabase 저장 순으로 처리됩니다.

**Request** `multipart/form-data`
| Field | Type | Description |
|-------|------|-------------|
| `file` | File | PDF 파일 |

**Query Params**
| Param | Default | Description |
|-------|---------|-------------|
| `force` | `false` | `true`로 설정하면 캐시 무시하고 재추출 |

**Response**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "processing",
  "cached": false,
  "filename": "attention_is_all_you_need.pdf"
}
```

---

## GET `/jobs/{job_id}`

추출 작업의 상태를 확인합니다. `done` 이 될 때까지 polling하세요.

**Response (처리 중)**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "processing"
}
```

**Response (완료)**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "done",
  "paper_id": "uuid-of-paper",
  "filename": "attention_is_all_you_need.pdf",
  "total_equations": 6
}
```

**Polling 예시**
```javascript
async function waitForResult(job_id) {
  while (true) {
    const res = await fetch(`https://paper2run-production.up.railway.app/jobs/${job_id}`);
    const job = await res.json();
    if (job.status === "done") return job;
    if (job.status === "error") throw new Error(job.error);
    await new Promise(r => setTimeout(r, 2000));
  }
}
```

---

## GET `/papers`

저장된 논문 목록을 반환합니다.

**Response**
```json
[
  {
    "id": "uuid-of-paper",
    "filename": "attention_is_all_you_need.pdf",
    "title": "attention is all you need",
    "total_equations": 6,
    "created_at": "2025-04-27T12:00:00"
  }
]
```

---

## GET `/papers/{paper_id}/equations`

논문의 핵심 수식을 `keep_rank` 순서로 반환합니다.

**Query Params**
| Param | Description |
|-------|-------------|
| `role` | `model` \| `loss` \| `update` \| `inference` \| `definition` |

**Response**
```json
[
  {
    "id": "uuid",
    "eq_number": 1,
    "keep_rank": 1,
    "role": "model",
    "importance_hint": "high",
    "latex": "\\text{Attention}(Q,K,V) = \\text{softmax}\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V",
    "description": "Scaled dot-product attention mechanism",
    "context": "We define attention as...",
    "core_reason": "Central mechanism of the proposed Transformer architecture",
    "section_hint": "Method",
    "page": 3
  }
]
```

| Field | Description |
|-------|-------------|
| `keep_rank` | 핵심도 순위 (1 = 가장 핵심) |
| `role` | 수식의 역할 |
| `importance_hint` | `high` \| `medium` |
| `latex` | LaTeX 문자열 (KaTeX로 렌더링 가능) |
| `core_reason` | 이 수식이 핵심인 이유 |

---

## GET `/papers/{paper_id}/figures`

논문의 figure 목록을 반환합니다. `image_url` 로 크롭된 figure 이미지에 바로 접근할 수 있습니다.

**Response**
```json
[
  {
    "id": "uuid",
    "paper_id": "uuid-of-paper",
    "fig_number": 1,
    "figure_id": "p3_fig1",
    "page": 3,
    "caption": "Figure 1: Overview of the proposed architecture.",
    "figure_type": "architecture_diagram",
    "page_bbox": [0.05, 0.12, 0.95, 0.55],
    "image_url": "/crops/attention_is_all_you_need/p3_fig1/_figure.jpg",
    "key_insight": "Encoder-decoder structure with multi-head attention.",
    "created_at": "2025-04-27T12:00:00"
  }
]
```

| Field | Description |
|-------|-------------|
| `figure_id` | `p{page}_{fig_id}` 형식의 고유 식별자 |
| `figure_type` | `method_overview` \| `architecture_diagram` \| `flowchart` \| `framework` \| `other` |
| `page_bbox` | 페이지 내 위치 `[x0, y0, x1, y1]` (0–1 normalized) |
| `image_url` | 크롭 이미지 경로. `{Base URL}{image_url}` 로 이미지 접근 가능 |
| `key_insight` | figure가 보여주는 핵심 내용 한 줄 요약 |

---

## GET `/health`

서버 상태 확인용입니다.

```json
{ "status": "ok" }
```
