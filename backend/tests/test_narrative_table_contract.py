from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.opendart.prepare_narrative import extract_markup_sections
from samnik_backend.agents import RagFaqAgent
from samnik_backend.domain import Company, Evidence, EvidenceStatus, Verdict


def test_ds_table_cell_extraction_contradicts_loss_claim() -> None:
    markup = b"""
    <html><body><h2>30. DS segment report</h2>
    <table><tr><td>DS</td><td>Operating profit</td><td>24,858,075</td></tr></table>
    </body></html>
    """.replace(b"Operating profit", "영업이익".encode("utf-8"))
    sections = extract_markup_sections("note30.xml", markup)
    excerpt = "\n".join(section.text for section in sections)
    section_path = " > ".join(section.section_path for section in sections)
    evidence = Evidence(
        evidence_id="chunk:official:ds-table",
        company=Company.SAMSUNG_ELECTRONICS,
        business_year=2025,
        rcept_no="20260310002820",
        filing_title="사업보고서 (2025.12)",
        filed_at=date(2026, 3, 10),
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        excerpt=excerpt,
        section_path=section_path,
        source_kind="FILING_CHUNK",
        evidence_status=EvidenceStatus.OFFICIAL_API_VERIFIED,
    )

    verdict = RagFaqAgent._narrative_verdict(
        "삼성전자 DS 부문이 2025년에도 적자였다는 글이 사실이야?",
        [evidence],
    )

    assert "영업이익" in excerpt
    assert verdict == Verdict.CONTRADICTED
