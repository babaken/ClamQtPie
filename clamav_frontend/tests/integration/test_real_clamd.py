"""
テスト仕様書3.1節: CLM-I01（実clamd + EICARファイルでの結合テスト）

本コンテナ環境では clamd が導入できないため（README_DEV.md 4章参照）、
tests/conftest.py の requires_real_clamd マーカーにより自動的にスキップされる。
実開発機（clamd導入済み）でこのファイルを実行すると本テストが実施される。
"""
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from tests.conftest import requires_real_clamd


@requires_real_clamd
def test_eicar_file_is_detected_by_real_clamd(eicar_file):
    config = ClamdConfig(connection_type="auto")
    service = ClamAVService(config)

    assert service.ping() is True

    results = service.scan_file(str(eicar_file))
    assert len(results) == 1
    assert results[0].status == "FOUND"
    assert "Eicar" in results[0].detail
