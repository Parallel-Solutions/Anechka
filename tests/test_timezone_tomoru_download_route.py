from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch


def test_tomoru_download_returns_zip_media_type(client):
    export_dir = Path.cwd()
    archive_path = export_dir / "test_timezone_route.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Europe_Moscow.csv", "phone_number\n79161234567\n")
    try:
        with (
            patch("app.routers.exports.LprTomoruService") as service_class,
            patch("app.routers.exports.get_export_dir", return_value=export_dir),
            patch("app.routers.exports.load_lpr_config"),
            patch("app.routers.exports.resolve_portal_id", return_value="test.portal"),
        ):
            service = service_class.return_value
            service.run_lpr_tomoru_export.return_value = str(archive_path)
            service.last_matched_total = 1
            response = client.post(
                "/exports/tomoru/download",
                json={
                    "entity_type": "deal",
                    "category_id": 15,
                    "group_by_timezone": True,
                    "local_call_time": "10:00",
                },
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["x-export-matched-total"] == "1"
        assert response.content.startswith(b"PK")
    finally:
        archive_path.unlink(missing_ok=True)
