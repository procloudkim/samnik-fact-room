BEGIN;

-- This seed is intentionally ineligible for production-like fact confirmation.
-- Values have been checked in official DART HTML, but no OpenDART API payload is
-- represented by this fixture and all evidence_status values remain synthetic.

INSERT INTO snapshot_import (
    snapshot_id,
    manifest_schema_version,
    source_system,
    acquisition_mode,
    evidence_status,
    acquired_at,
    manifest_path,
    manifest_json
)
VALUES (
    'e590ffd1306bc019a7c95db2e00623aaf524e19d7db576810573dfed8452725b',
    '1.0',
    'OpenDART',
    'SYNTHETIC_FIXTURE',
    'SYNTHETIC_UNVERIFIED',
    '2026-07-19T00:00:00+09:00',
    'data/fixtures/synthetic/manifest.json',
    '{"fixture":true,"evidence_status":"SYNTHETIC_UNVERIFIED","warning":"Not an OpenDART API capture; never emit CONFIRMED."}'::jsonb
)
ON CONFLICT DO NOTHING;

INSERT INTO company (corp_code, stock_code, corp_name, corp_name_en)
VALUES
    ('00126380', '005930', '삼성전자', 'Samsung Electronics Co., Ltd.'),
    ('00164779', '000660', 'SK하이닉스', 'SK hynix Inc.')
ON CONFLICT DO NOTHING;

INSERT INTO filing (
    rcept_no,
    snapshot_id,
    corp_code,
    report_name,
    business_year,
    reprt_code,
    fs_div,
    period_start,
    period_end,
    filing_date,
    source_url,
    document_sha256,
    evidence_status
)
VALUES
    (
        '20260310002820',
        'e590ffd1306bc019a7c95db2e00623aaf524e19d7db576810573dfed8452725b',
        '00126380',
        '사업보고서 (2025.12)',
        2025,
        '11011',
        'CFS',
        '2025-01-01',
        '2025-12-31',
        '2026-03-10',
        'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820',
        NULL,
        'SYNTHETIC_UNVERIFIED'
    ),
    (
        '20260317000635',
        'e590ffd1306bc019a7c95db2e00623aaf524e19d7db576810573dfed8452725b',
        '00164779',
        '사업보고서 (2025.12)',
        2025,
        '11011',
        'CFS',
        '2025-01-01',
        '2025-12-31',
        '2026-03-17',
        'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260317000635',
        NULL,
        'SYNTHETIC_UNVERIFIED'
    )
ON CONFLICT DO NOTHING;

INSERT INTO financial_fact (
    evidence_id,
    snapshot_id,
    rcept_no,
    corp_code,
    business_year,
    metric_key,
    account_id,
    account_nm,
    sj_div,
    fs_div,
    period_start,
    period_end,
    value_scope,
    raw_amount,
    amount_numeric,
    currency,
    source_field,
    unit_scale,
    source_url,
    evidence_status,
    value_verification_status,
    verified_at
)
VALUES
    (
        'fixture:fact:samsung:2025:operating_profit',
        'e590ffd1306bc019a7c95db2e00623aaf524e19d7db576810573dfed8452725b',
        '20260310002820',
        '00126380',
        2025,
        'operating_profit',
        'ifrs-full_ProfitLossFromOperatingActivities',
        '영업이익',
        'IS',
        'CFS',
        '2025-01-01',
        '2025-12-31',
        'CONSOLIDATED_ANNUAL',
        '43,601,051',
        43601051000000,
        'KRW',
        'SYNTHETIC_PDF_EXPECTATION',
        1000000,
        'https://dart.fss.or.kr/report/viewer.do?rcpNo=20260310002820&dcmNo=11104488&eleId=21&offset=432032&length=19681&dtd=dart4.xsd',
        'SYNTHETIC_UNVERIFIED',
        'OFFICIAL_DART_HTML_VERIFIED',
        '2026-07-19T00:00:00+09:00'
    ),
    (
        'fixture:fact:sk-hynix:2025:operating_profit',
        'e590ffd1306bc019a7c95db2e00623aaf524e19d7db576810573dfed8452725b',
        '20260317000635',
        '00164779',
        2025,
        'operating_profit',
        'ifrs-full_ProfitLossFromOperatingActivities',
        '영업이익',
        'IS',
        'CFS',
        '2025-01-01',
        '2025-12-31',
        'CONSOLIDATED_ANNUAL',
        '47,206,319',
        47206319000000,
        'KRW',
        'SYNTHETIC_PDF_EXPECTATION',
        1000000,
        'https://dart.fss.or.kr/report/viewer.do?rcpNo=20260317000635&dcmNo=11132576&eleId=21&offset=371607&length=31668&dtd=dart4.xsd',
        'SYNTHETIC_UNVERIFIED',
        'OFFICIAL_DART_HTML_VERIFIED',
        '2026-07-19T00:00:00+09:00'
    )
ON CONFLICT DO NOTHING;

INSERT INTO source_chunk (
    chunk_id,
    snapshot_id,
    rcept_no,
    corp_code,
    business_year,
    section_path,
    chunk_no,
    content,
    content_sha256,
    embedding_model,
    embedding,
    source_url,
    evidence_status,
    value_verification_status,
    verified_at
)
VALUES (
    'd759971c22734eb89554e59d78e0d6088fb1e756d4b1e89b69c3033f9787ebcf',
    'e590ffd1306bc019a7c95db2e00623aaf524e19d7db576810573dfed8452725b',
    '20260310002820',
    '00126380',
    2025,
    '연결재무제표 주석 > 30. 부문별 보고 > DS 부문',
    0,
    '[합성 테스트 청크 - 원문 인용 아님] 2025년 DS 부문 영업이익 테스트 값은 24,858,075백만원으로 양(+)의 값이다. 이 수치는 삼성전자 전체 또는 메모리 제품만의 실적과 동일하지 않다.',
    'd759971c22734eb89554e59d78e0d6088fb1e756d4b1e89b69c3033f9787ebcf',
    'synthetic-fixture-384-v1',
    (
        '[' || array_to_string(
            array_prepend(1::double precision, array_fill(0::double precision, ARRAY[383])),
            ','
        ) || ']'
    )::vector,
    'https://dart.fss.or.kr/report/viewer.do?rcpNo=20260310002820&dcmNo=11104488&eleId=55&offset=2420698&length=94132&dtd=dart4.xsd',
    'SYNTHETIC_UNVERIFIED',
    'OFFICIAL_DART_HTML_VERIFIED',
    '2026-07-19T00:00:00+09:00'
)
ON CONFLICT DO NOTHING;

COMMIT;
