CREATE TABLE IF NOT EXISTS user_settings (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    reminder_type TEXT NOT NULL DEFAULT 'calorie_under_goal',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, date, reminder_type)
);

CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_date
    ON reminder_deliveries (date DESC);

CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_user_date
    ON reminder_deliveries (user_id, date DESC);
