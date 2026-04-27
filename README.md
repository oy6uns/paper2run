# Paper2Run API

PDF 논문에서 핵심 수식을 자동으로 추출하는 API 서버입니다.

**Base URL:** `https://paper2run-production.up.railway.app`

---

## Flow

```
POST /papers/extract   →   GET /jobs/{job_id}   →   GET /papers/{paper_id}/equations
  (PDF 업로드)               (완료 대기 polling)          (수식 데이터 사용)
```

같은 파일명의 논문이 이미 DB에 있으면 GPT 호출 없이 즉시 반환됩니다 (`cached: true`).

---

## Endpoints

### POST `/papers/extract`
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

### GET `/jobs/{job_id}`
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

### GET `/papers`
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

### GET `/papers/{paper_id}/equations`
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

### GET `/health`
서버 상태 확인용 엔드포인트입니다.

```json
{ "status": "ok" }
```
