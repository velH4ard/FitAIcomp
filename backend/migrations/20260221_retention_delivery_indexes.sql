CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_user_type_date
    ON reminder_deliveries (user_id, reminder_type, date DESC);
