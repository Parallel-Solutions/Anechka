"""Create a non-mutating Tomoru ZIP from dry-run retry campaign drafts."""

from __future__ import annotations

import csv
import io
import zipfile

from app.services.call_results.tomoru_retry_campaign import TomoruCampaignDraft


def build_tomoru_retry_zip(drafts: list[TomoruCampaignDraft]) -> bytes:
    output = io.BytesIO()
    manifest_io = io.StringIO(newline="")
    manifest = csv.writer(manifest_io, delimiter=";")
    manifest.writerow(
        [
            "order",
            "campaign_name",
            "timezone",
            "scheduled_at",
            "local_call_time",
            "reason",
            "contact_count",
            "filename",
            "idempotency_key",
        ]
    )

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, draft in enumerate(drafts, start=1):
            filename = f"{index:03d}_{draft.campaign_name}.csv"
            contacts_io = io.StringIO(newline="")
            contacts = csv.writer(contacts_io)
            contacts.writerow(
                [
                    "phone_number",
                    "phone_extension",
                    "queue_entry_id",
                    "deal_id",
                    "contact_id",
                    "callback_text",
                ]
            )
            for contact in draft.contacts:
                contacts.writerow(
                    [
                        contact.phone_number,
                        contact.phone_extension or "",
                        contact.queue_entry_id,
                        contact.deal_id or "",
                        contact.contact_id or "",
                        contact.callback_text or "",
                    ]
                )
            archive.writestr(filename, contacts_io.getvalue().encode("utf-8-sig"))
            manifest.writerow(
                [
                    index,
                    draft.campaign_name,
                    draft.timezone,
                    draft.scheduled_at.isoformat(),
                    draft.scheduled_at.strftime("%H:%M"),
                    draft.reason,
                    len(draft.contacts),
                    filename,
                    draft.idempotency_key,
                ]
            )

        archive.writestr("manifest.csv", manifest_io.getvalue().encode("utf-8-sig"))
        archive.writestr(
            "README.txt",
            (
                "ПАКЕТ ПОВТОРНЫХ И ОБРАТНЫХ ЗВОНКОВ ДЛЯ TOMORU\n"
                "\n"
                "Этот пакет не отправлен в Bitrix24 и Tomoru.\n"
                "Сначала откройте manifest.csv и проверьте порядок кампаний, их время и соответствующий часовой пояс.\n"
                "Затем загрузите подготовленные CSV-файлы вручную, проверьте агента, время, часовой пояс и тестовый номер. Не запускайте массовый обзвон без проверки.\n"
            ).encode("utf-8-sig"),
        )
    return output.getvalue()
