-- 0004_session_confirm_failures
-- Decision-log Entry 33 (per-session confirmation limit).
-- Forward-only, additive: the pre-0004 image ignores the column and its inserts get 0.

-- Wrong password confirmations on the signed-in routes (password change,
-- recovery-code rotation), counted per session. The threshold (10) lives in code.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS failed_confirmations integer NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sessions_failed_confirmations_check' AND conrelid = 'sessions'::regclass) THEN
        ALTER TABLE sessions ADD CONSTRAINT sessions_failed_confirmations_check CHECK (failed_confirmations >= 0);
    END IF;
END
$$;
