BEGIN;

CREATE TABLE IF NOT EXISTS snapshot_import (
    snapshot_id char(64) PRIMARY KEY,
    manifest_schema_version text NOT NULL,
    source_system text NOT NULL CHECK (source_system = 'OpenDART'),
    acquisition_mode text NOT NULL CHECK (
        acquisition_mode IN ('LIVE_API', 'SYNTHETIC_FIXTURE')
    ),
    evidence_status text NOT NULL CHECK (
        evidence_status IN (
            'SYNTHETIC_UNVERIFIED',
            'OFFICIAL_API_VERIFIED',
            'REJECTED'
        )
    ),
    acquired_at timestamptz NOT NULL,
    manifest_path text NOT NULL,
    manifest_json jsonb NOT NULL,
    immutable boolean NOT NULL DEFAULT true CHECK (immutable),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        acquisition_mode <> 'SYNTHETIC_FIXTURE'
        OR evidence_status = 'SYNTHETIC_UNVERIFIED'
    )
);

CREATE TABLE IF NOT EXISTS company (
    corp_code char(8) PRIMARY KEY CHECK (corp_code ~ '^[0-9]{8}$'),
    stock_code char(6) UNIQUE CHECK (stock_code ~ '^[0-9]{6}$'),
    corp_name text NOT NULL,
    corp_name_en text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS filing (
    rcept_no char(14) NOT NULL CHECK (rcept_no ~ '^[0-9]{14}$'),
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    corp_code char(8) NOT NULL REFERENCES company(corp_code),
    report_name text NOT NULL,
    business_year smallint NOT NULL CHECK (business_year BETWEEN 1999 AND 2100),
    reprt_code char(5) NOT NULL,
    fs_div text NOT NULL CHECK (fs_div IN ('CFS', 'OFS')),
    period_start date NOT NULL,
    period_end date NOT NULL,
    filing_date date NOT NULL,
    source_url text NOT NULL CHECK (source_url LIKE 'https://dart.fss.or.kr/%'),
    document_sha256 char(64) CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_status text NOT NULL CHECK (
        evidence_status IN ('SYNTHETIC_UNVERIFIED', 'OFFICIAL_API_VERIFIED')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (period_start <= period_end),
    CHECK (evidence_status <> 'OFFICIAL_API_VERIFIED' OR document_sha256 IS NOT NULL),
    PRIMARY KEY (snapshot_id, rcept_no)
);

CREATE INDEX IF NOT EXISTS filing_receipt_idx ON filing (rcept_no);

CREATE TABLE IF NOT EXISTS financial_fact (
    financial_fact_id bigserial PRIMARY KEY,
    evidence_id text NOT NULL UNIQUE,
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    rcept_no char(14) NOT NULL,
    corp_code char(8) NOT NULL REFERENCES company(corp_code),
    business_year smallint NOT NULL CHECK (business_year BETWEEN 1999 AND 2100),
    metric_key text NOT NULL,
    account_id text NOT NULL,
    account_nm text NOT NULL,
    sj_div text NOT NULL,
    fs_div text NOT NULL CHECK (fs_div = 'CFS'),
    period_start date NOT NULL,
    period_end date NOT NULL,
    value_scope text NOT NULL,
    raw_amount text NOT NULL,
    amount_numeric numeric(30, 0) NOT NULL,
    currency char(3) NOT NULL DEFAULT 'KRW' CHECK (currency = 'KRW'),
    source_field text NOT NULL,
    unit_scale bigint NOT NULL DEFAULT 1 CHECK (unit_scale > 0),
    source_url text NOT NULL CHECK (source_url LIKE 'https://dart.fss.or.kr/%'),
    evidence_status text NOT NULL CHECK (
        evidence_status IN ('SYNTHETIC_UNVERIFIED', 'OFFICIAL_API_VERIFIED')
    ),
    value_verification_status text NOT NULL DEFAULT 'UNVERIFIED' CHECK (
        value_verification_status IN ('UNVERIFIED', 'OFFICIAL_DART_HTML_VERIFIED')
    ),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (period_start <= period_end),
    FOREIGN KEY (snapshot_id, rcept_no) REFERENCES filing(snapshot_id, rcept_no),
    CHECK (
        value_verification_status <> 'OFFICIAL_DART_HTML_VERIFIED'
        OR verified_at IS NOT NULL
    ),
    UNIQUE (
        snapshot_id,
        corp_code,
        business_year,
        metric_key,
        fs_div,
        period_start,
        period_end,
        value_scope
    )
);

CREATE INDEX IF NOT EXISTS financial_fact_lookup_idx
    ON financial_fact (
        corp_code,
        business_year,
        metric_key,
        fs_div,
        period_start,
        period_end,
        evidence_status
    );

CREATE TABLE IF NOT EXISTS source_chunk (
    chunk_id char(64) PRIMARY KEY CHECK (chunk_id ~ '^[0-9a-f]{64}$'),
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    rcept_no char(14) NOT NULL,
    corp_code char(8) NOT NULL REFERENCES company(corp_code),
    business_year smallint NOT NULL CHECK (business_year BETWEEN 1999 AND 2100),
    section_path text NOT NULL,
    chunk_no integer NOT NULL CHECK (chunk_no >= 0),
    content text NOT NULL CHECK (length(content) > 0),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    embedding_model text NOT NULL,
    embedding vector(384) NOT NULL,
    source_url text NOT NULL CHECK (source_url LIKE 'https://dart.fss.or.kr/%'),
    evidence_status text NOT NULL CHECK (
        evidence_status IN ('SYNTHETIC_UNVERIFIED', 'OFFICIAL_API_VERIFIED')
    ),
    value_verification_status text NOT NULL DEFAULT 'UNVERIFIED' CHECK (
        value_verification_status IN ('UNVERIFIED', 'OFFICIAL_DART_HTML_VERIFIED')
    ),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        value_verification_status <> 'OFFICIAL_DART_HTML_VERIFIED'
        OR verified_at IS NOT NULL
    ),
    FOREIGN KEY (snapshot_id, rcept_no) REFERENCES filing(snapshot_id, rcept_no),
    UNIQUE (snapshot_id, rcept_no, section_path, chunk_no)
);

CREATE INDEX IF NOT EXISTS source_chunk_metadata_idx
    ON source_chunk (
        corp_code,
        business_year,
        rcept_no,
        evidence_status,
        section_path
    );

CREATE TABLE IF NOT EXISTS answer_evidence (
    evidence_id text PRIMARY KEY,
    answer_id text NOT NULL,
    snapshot_id char(64) NOT NULL REFERENCES snapshot_import(snapshot_id),
    rcept_no char(14) NOT NULL,
    financial_fact_id bigint REFERENCES financial_fact(financial_fact_id),
    chunk_id char(64) REFERENCES source_chunk(chunk_id),
    evidence_type text NOT NULL CHECK (evidence_type IN ('FINANCIAL_FACT', 'SOURCE_CHUNK')),
    quote_text text,
    source_url text NOT NULL CHECK (source_url LIKE 'https://dart.fss.or.kr/%'),
    evidence_status text NOT NULL CHECK (
        evidence_status IN ('SYNTHETIC_UNVERIFIED', 'OFFICIAL_API_VERIFIED')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(financial_fact_id, chunk_id) = 1),
    CHECK (
        (evidence_type = 'FINANCIAL_FACT' AND financial_fact_id IS NOT NULL)
        OR (evidence_type = 'SOURCE_CHUNK' AND chunk_id IS NOT NULL)
    ),
    FOREIGN KEY (snapshot_id, rcept_no) REFERENCES filing(snapshot_id, rcept_no)
);

CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id text PRIMARY KEY,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_snapshot_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable; import a new snapshot instead', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS snapshot_import_immutable ON snapshot_import;
CREATE TRIGGER snapshot_import_immutable
BEFORE UPDATE OR DELETE ON snapshot_import
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS filing_immutable ON filing;
CREATE TRIGGER filing_immutable
BEFORE UPDATE OR DELETE ON filing
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS financial_fact_immutable ON financial_fact;
CREATE TRIGGER financial_fact_immutable
BEFORE UPDATE OR DELETE ON financial_fact
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

DROP TRIGGER IF EXISTS source_chunk_immutable ON source_chunk;
CREATE TRIGGER source_chunk_immutable
BEFORE UPDATE OR DELETE ON source_chunk
FOR EACH ROW EXECUTE FUNCTION reject_snapshot_evidence_mutation();

COMMIT;
