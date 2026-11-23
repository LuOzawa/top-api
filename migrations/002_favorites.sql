-- 002: 收藏表（W3 写路径）
CREATE TABLE IF NOT EXISTS favorites (
  id         BIGSERIAL PRIMARY KEY,
  owner      TEXT NOT NULL DEFAULT 'me',
  url        TEXT NOT NULL,
  title      TEXT,
  platform   TEXT,
  note       TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT favorites_owner_url_key UNIQUE (owner, url)
);

CREATE INDEX IF NOT EXISTS idx_fav_owner_created ON favorites (owner, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fav_platform ON favorites (platform);
