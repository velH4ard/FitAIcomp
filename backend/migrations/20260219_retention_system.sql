ALTER TABLE users
    ADD COLUMN IF NOT EXISTS daily_goal_auto INT NOT NULL DEFAULT 2000;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS daily_goal_override INT;

UPDATE users
SET daily_goal_auto = 2000
WHERE daily_goal_auto IS NULL OR daily_goal_auto <= 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'users_daily_goal_override_range'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_daily_goal_override_range
            CHECK (
                daily_goal_override IS NULL
                OR (daily_goal_override >= 1000 AND daily_goal_override <= 5000)
            );
    END IF;
END $$;

ALTER TABLE reminder_deliveries
    ALTER COLUMN reminder_type SET DEFAULT 'daily_progress';
