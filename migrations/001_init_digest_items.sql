-- 001: 日报明细表（从 SQLite dg_items 迁移过来的数据）
CREATE TABLE IF NOT EXISTS digest_items (
  day         DATE        NOT NULL,
  slot        TEXT        NOT NULL,
  platform    TEXT        NOT NULL,
  item_id     TEXT        NOT NULL,
  title       TEXT,
  url         TEXT,
  points      INTEGER     DEFAULT 0,
  comments    INTEGER     DEFAULT 0,
  fetched_at  TIMESTAMPTZ,
  migrated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (day, slot, platform, item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_day_platform ON digest_items (day, platform);
CREATE INDEX IF NOT EXISTS idx_items_platform_points ON digest_items (platform, points DESC);
CREATE INDEX IF NOT EXISTS idx_items_url ON digest_items (url);
