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

-- ③ figures 테이블: figure 하나당 한 행
CREATE TABLE IF NOT EXISTS figures (
  id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  paper_id    UUID        REFERENCES papers(id) ON DELETE CASCADE,
  fig_number  INTEGER,
  figure_id   TEXT,                    -- 논문 내 figure 번호 (e.g. "fig1")
  page        INTEGER,
  caption     TEXT,
  figure_type TEXT,                    -- architecture_diagram / line_plot / ...
  page_bbox   FLOAT[]     DEFAULT '{}', -- [x0, y0, x1, y1] 0-1 fraction of full page
  image_url   TEXT,                    -- /crops/{paper}/{fig_id}/_figure.jpg
  layout      TEXT,
  key_insight TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_figures_paper_id ON figures(paper_id);

-- Supabase migration (기존 DB에 적용):
-- ALTER TABLE figures ADD COLUMN IF NOT EXISTS image_url TEXT;
-- ALTER TABLE figures DROP COLUMN IF EXISTS components;
-- DROP TABLE IF EXISTS figure_components;
