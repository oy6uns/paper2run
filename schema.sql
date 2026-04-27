-- ① papers 테이블: PDF 논문 하나당 한 행
CREATE TABLE IF NOT EXISTS papers (
  id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  filename         TEXT        NOT NULL UNIQUE,
  title            TEXT,
  total_equations  INTEGER     DEFAULT 0,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ② equations 테이블: 수식 하나당 한 행
CREATE TABLE IF NOT EXISTS equations (
  id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  paper_id    UUID        REFERENCES papers(id) ON DELETE CASCADE,
  eq_number   INTEGER,
  page        INTEGER,
  latex       TEXT        NOT NULL,
  description TEXT,
  context     TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 웹앱에서 자주 쓸 인덱스
CREATE INDEX IF NOT EXISTS idx_equations_paper_id ON equations(paper_id);
