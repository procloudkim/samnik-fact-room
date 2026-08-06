BEGIN;

ALTER TABLE snapshot_import
    ADD COLUMN IF NOT EXISTS narrative_corpus_status text NOT NULL
        DEFAULT 'NOT_BUILT',
    ADD COLUMN IF NOT EXISTS narrative_covered_files integer NOT NULL
        DEFAULT 0 CHECK (narrative_covered_files >= 0),
    ADD COLUMN IF NOT EXISTS narrative_total_files integer NOT NULL
        DEFAULT 0 CHECK (narrative_total_files >= 0);

-- Earlier local pre-release runs used an unnamed inline status constraint.
ALTER TABLE snapshot_import
    DROP CONSTRAINT IF EXISTS snapshot_import_narrative_corpus_status_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'narrative_corpus_status_valid'
          AND conrelid = 'snapshot_import'::regclass
    ) THEN
        ALTER TABLE snapshot_import
            ADD CONSTRAINT narrative_corpus_status_valid CHECK (
                narrative_corpus_status IN (
                    'NOT_BUILT', 'PARTIAL_TEXT', 'FULL_TEXT'
                )
            );
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'narrative_coverage_counts_valid'
          AND conrelid = 'snapshot_import'::regclass
    ) THEN
        ALTER TABLE snapshot_import
            ADD CONSTRAINT narrative_coverage_counts_valid CHECK (
                narrative_covered_files <= narrative_total_files
                AND (
                    narrative_corpus_status <> 'FULL_TEXT'
                    OR (
                        narrative_total_files > 0
                        AND narrative_covered_files = narrative_total_files
                    )
                )
            );
    END IF;
END;
$$;

COMMIT;
