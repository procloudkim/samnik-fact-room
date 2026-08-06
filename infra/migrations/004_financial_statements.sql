BEGIN;

CREATE TABLE IF NOT EXISTS filing_revision (
    filing_revision_id bigserial PRIMARY KEY,
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    rcept_no char(14) NOT NULL CHECK (rcept_no ~ '^[0-9]{14}$'),
    corp_code char(8) NOT NULL REFERENCES company(corp_code),
    business_year smallint NOT NULL CHECK (business_year BETWEEN 2015 AND 2100),
    reprt_code char(5) NOT NULL CHECK (reprt_code IN ('11013', '11012', '11014', '11011')),
    filing_date date NOT NULL,
    correction_kind text NOT NULL DEFAULT 'ORIGINAL',
    selected_latest boolean NOT NULL DEFAULT true,
    source_url text NOT NULL CHECK (source_url LIKE 'https://dart.fss.or.kr/%'),
    revision_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, rcept_no)
);

INSERT INTO filing_revision (
    snapshot_id, rcept_no, corp_code, business_year, reprt_code,
    filing_date, correction_kind, selected_latest, source_url, revision_json
)
SELECT f.snapshot_id, f.rcept_no, f.corp_code, f.business_year, f.reprt_code,
       f.filing_date,
       CASE WHEN f.report_name LIKE '%%정정%%' THEN 'CORRECTION' ELSE 'ORIGINAL' END,
       true, f.source_url,
       jsonb_build_object('report_name', f.report_name, 'migrated_from', 'filing')
FROM filing f
ON CONFLICT (snapshot_id, rcept_no) DO NOTHING;

CREATE TABLE IF NOT EXISTS financial_statement_line (
    financial_statement_line_id bigserial PRIMARY KEY,
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    filing_revision_id bigint NOT NULL REFERENCES filing_revision(filing_revision_id),
    rcept_no char(14) NOT NULL,
    corp_code char(8) NOT NULL REFERENCES company(corp_code),
    business_year smallint NOT NULL CHECK (business_year BETWEEN 2015 AND 2100),
    reprt_code char(5) NOT NULL CHECK (reprt_code IN ('11013', '11012', '11014', '11011')),
    fs_div text NOT NULL CHECK (fs_div IN ('CFS', 'OFS')),
    sj_div text NOT NULL CHECK (sj_div IN ('BS', 'IS', 'CIS', 'CF', 'SCE')),
    account_id text NOT NULL,
    account_nm text NOT NULL,
    account_detail text,
    ord integer NOT NULL CHECK (ord >= 0),
    currency char(3) NOT NULL DEFAULT 'KRW' CHECK (currency = 'KRW'),
    thstrm_nm text,
    thstrm_amount_raw text,
    thstrm_amount numeric(38, 0),
    thstrm_add_amount_raw text,
    thstrm_add_amount numeric(38, 0),
    frmtrm_nm text,
    frmtrm_amount_raw text,
    frmtrm_amount numeric(38, 0),
    frmtrm_q_nm text,
    frmtrm_q_amount_raw text,
    frmtrm_q_amount numeric(38, 0),
    frmtrm_add_amount_raw text,
    frmtrm_add_amount numeric(38, 0),
    bfefrmtrm_nm text,
    bfefrmtrm_amount_raw text,
    bfefrmtrm_amount numeric(38, 0),
    raw_json jsonb NOT NULL,
    row_hash char(64) NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, rcept_no, fs_div, row_hash),
    FOREIGN KEY (snapshot_id, rcept_no)
        REFERENCES filing_revision(snapshot_id, rcept_no)
);

CREATE TABLE IF NOT EXISTS metric_alias (
    metric_alias_id bigserial PRIMARY KEY,
    metric_key text NOT NULL CHECK (metric_key IN (
        'revenue', 'operating_profit', 'net_income', 'total_assets',
        'total_liabilities', 'total_equity', 'operating_cash_flow'
    )),
    corp_code char(8) REFERENCES company(corp_code),
    account_id text,
    exact_account_nm text,
    valid_from_year smallint NOT NULL DEFAULT 2015 CHECK (valid_from_year >= 2015),
    valid_to_year smallint CHECK (valid_to_year IS NULL OR valid_to_year >= valid_from_year),
    comparison_grade text NOT NULL DEFAULT 'REVIEWED' CHECK (
        comparison_grade IN ('EXACT', 'REVIEWED', 'COMPANY_ONLY', 'NOT_COMPARABLE')
    ),
    priority smallint NOT NULL DEFAULT 100 CHECK (priority >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (account_id IS NOT NULL OR exact_account_nm IS NOT NULL),
    UNIQUE NULLS NOT DISTINCT (
        metric_key, corp_code, account_id, exact_account_nm, valid_from_year
    )
);

CREATE TABLE IF NOT EXISTS data_coverage (
    data_coverage_id bigserial PRIMARY KEY,
    snapshot_id char(64) REFERENCES snapshot_import(snapshot_id),
    corp_code char(8) NOT NULL REFERENCES company(corp_code),
    business_year smallint NOT NULL CHECK (business_year BETWEEN 2015 AND 2100),
    reprt_code char(5) NOT NULL CHECK (reprt_code IN ('11013', '11012', '11014', '11011')),
    fs_div text NOT NULL CHECK (fs_div IN ('CFS', 'OFS')),
    status text NOT NULL CHECK (status IN (
        'LOADED', 'NOT_FILED', 'NOT_YET_FILED', 'SOURCE_NOT_AVAILABLE', 'FAILED'
    )),
    api_status_code text,
    rcept_no char(14),
    source_row_count integer NOT NULL DEFAULT 0 CHECK (source_row_count >= 0),
    loaded_row_count integer NOT NULL DEFAULT 0 CHECK (loaded_row_count >= 0),
    checked_at timestamptz NOT NULL,
    detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'LOADED' OR (
        snapshot_id IS NOT NULL AND rcept_no IS NOT NULL
        AND source_row_count = loaded_row_count
    )),
    UNIQUE NULLS NOT DISTINCT (
        snapshot_id, corp_code, business_year, reprt_code, fs_div, checked_at
    )
);

CREATE TABLE IF NOT EXISTS narrative_coverage (
    narrative_coverage_id bigserial PRIMARY KEY,
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    filing_revision_id bigint NOT NULL REFERENCES filing_revision(filing_revision_id),
    status text NOT NULL CHECK (status IN (
        'FULL_TEXT', 'PARTIAL_TEXT', 'SOURCE_NOT_AVAILABLE'
    )),
    retrievable_file_count integer NOT NULL DEFAULT 0 CHECK (retrievable_file_count >= 0),
    processed_file_count integer NOT NULL DEFAULT 0 CHECK (processed_file_count >= 0),
    chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    checked_at timestamptz NOT NULL,
    detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (processed_file_count <= retrievable_file_count),
    CHECK (status <> 'FULL_TEXT' OR processed_file_count = retrievable_file_count),
    UNIQUE (snapshot_id, filing_revision_id)
);

CREATE INDEX IF NOT EXISTS filing_revision_lookup_idx
    ON filing_revision (corp_code, business_year, reprt_code, filing_date DESC, rcept_no DESC);
CREATE INDEX IF NOT EXISTS financial_statement_line_lookup_idx
    ON financial_statement_line (
        corp_code, business_year, reprt_code, fs_div, sj_div, account_id, account_nm
    );
CREATE INDEX IF NOT EXISTS financial_statement_line_name_trgm_idx
    ON financial_statement_line USING gin (to_tsvector('simple', account_nm));
CREATE INDEX IF NOT EXISTS metric_alias_lookup_idx
    ON metric_alias (metric_key, corp_code, valid_from_year, priority);
CREATE INDEX IF NOT EXISTS data_coverage_lookup_idx
    ON data_coverage (corp_code, business_year, reprt_code, fs_div, checked_at DESC);
CREATE INDEX IF NOT EXISTS narrative_coverage_lookup_idx
    ON narrative_coverage (filing_revision_id, status);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'source_chunk_filing_revision_fk'
          AND conrelid = 'source_chunk'::regclass
    ) THEN
        ALTER TABLE source_chunk
            ADD CONSTRAINT source_chunk_filing_revision_fk
            FOREIGN KEY (snapshot_id, rcept_no)
            REFERENCES filing_revision(snapshot_id, rcept_no);
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS filing_revision_immutable ON filing_revision;
CREATE TRIGGER filing_revision_immutable
BEFORE UPDATE OR DELETE ON filing_revision
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS financial_statement_line_immutable ON financial_statement_line;
CREATE TRIGGER financial_statement_line_immutable
BEFORE UPDATE OR DELETE ON financial_statement_line
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS metric_alias_immutable ON metric_alias;
CREATE TRIGGER metric_alias_immutable
BEFORE UPDATE OR DELETE ON metric_alias
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS data_coverage_immutable ON data_coverage;
CREATE TRIGGER data_coverage_immutable
BEFORE UPDATE OR DELETE ON data_coverage
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS narrative_coverage_immutable ON narrative_coverage;
CREATE TRIGGER narrative_coverage_immutable
BEFORE UPDATE OR DELETE ON narrative_coverage
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

INSERT INTO metric_alias (metric_key, account_id, exact_account_nm, comparison_grade, priority)
VALUES
    ('revenue', 'ifrs-full_Revenue', '매출액', 'REVIEWED', 10),
    ('revenue', 'ifrs-full_Revenue', '수익(매출액)', 'REVIEWED', 20),
    ('operating_profit', 'ifrs-full_ProfitLossFromOperatingActivities', '영업이익', 'REVIEWED', 10),
    ('operating_profit', 'dart_OperatingIncomeLoss', '영업이익(손실)', 'REVIEWED', 20),
    ('net_income', 'ifrs-full_ProfitLoss', '당기순이익', 'REVIEWED', 10),
    ('total_assets', 'ifrs-full_Assets', '자산총계', 'EXACT', 10),
    ('total_liabilities', 'ifrs-full_Liabilities', '부채총계', 'EXACT', 10),
    ('total_equity', 'ifrs-full_Equity', '자본총계', 'EXACT', 10),
    ('operating_cash_flow', 'ifrs-full_CashFlowsFromUsedInOperatingActivities', '영업활동현금흐름', 'REVIEWED', 10)
ON CONFLICT DO NOTHING;

COMMIT;
