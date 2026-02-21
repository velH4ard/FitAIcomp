ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS notification_tone TEXT NOT NULL DEFAULT 'balanced';

UPDATE user_settings
SET notification_tone = 'balanced'
WHERE notification_tone IS NULL
   OR notification_tone NOT IN ('soft', 'hard', 'balanced');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'user_settings_notification_tone_allowed'
    ) THEN
        ALTER TABLE user_settings
            ADD CONSTRAINT user_settings_notification_tone_allowed
            CHECK (notification_tone IN ('soft', 'hard', 'balanced'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'daily_stats'
          AND indexdef ILIKE '%(user_id, date)%'
    ) THEN
        CREATE INDEX idx_daily_stats_user_date ON daily_stats (user_id, date);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.conrelid = 'reminder_deliveries'::regclass
          AND c.contype = 'u'
          AND (
              SELECT array_agg(att.attname::text ORDER BY ord.ordinality)
              FROM unnest(c.conkey) WITH ORDINALITY AS ord(attnum, ordinality)
              JOIN pg_attribute att
                ON att.attrelid = c.conrelid
               AND att.attnum = ord.attnum
          ) = ARRAY['user_id', 'date', 'reminder_type']::text[]
    ) THEN
        ALTER TABLE reminder_deliveries
            ADD CONSTRAINT reminder_deliveries_user_date_type_key UNIQUE (user_id, date, reminder_type);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_date
    ON reminder_deliveries (date DESC);

CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_user_date
    ON reminder_deliveries (user_id, date DESC);
