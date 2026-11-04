"""Seed tenant_templates + broadcasts for the polaris-owner demo tenant so the
Templates and Broadcasts pages aren't empty. Idempotent; tagged for cleanup.
NO secrets: components are generic message text, no tokens."""
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import asyncpg

T = "00000000-0000-0000-0000-000000000001"
NOW = datetime.now(UTC)

TEMPLATES = [
    ("appointment_reminder", "UTILITY", "Hi {{1}}, this is a reminder for your appointment on {{2}} at {{3}}. Reply RESCHEDULE if you need a new time."),
    ("booking_confirmation", "UTILITY", "Your booking is confirmed ✅ {{1}} on {{2}} at {{3}}. See you then!"),
    ("monthly_promo", "MARKETING", "🎉 This month only: a free 30-min consultation with every session booked. Reply BOOK to grab a slot."),
    ("welcome_message", "UTILITY", "Hi {{1}}! Thanks for reaching out. How can we help you today? You can ask about our services, pricing or book an appointment."),
]


async def main():
    c = await asyncpg.connect(os.environ["POLARIS_DB_URL_SERVICE"])

    wa = await c.fetchval(
        "SELECT id FROM tenant_channels WHERE tenant_id=$1 AND channel='whatsapp' ORDER BY created_at LIMIT 1",
        T,
    )
    if wa is None:
        wa = await c.fetchval("SELECT id FROM tenant_channels WHERE tenant_id=$1 LIMIT 1", T)
    print("channel:", wa)

    names = [n for (n, _, _) in TEMPLATES]
    # Idempotent cleanup keyed on the broadcast display names (no visible
    # "[mktg]" marker — these names show up in marketing screenshots). Also
    # clears any legacy "[mktg]"-tagged rows from older seed runs.
    broadcast_names = ["June Promo Blast", "Appointment Reminders"]
    await c.execute(
        "DELETE FROM broadcasts WHERE tenant_id=$1 AND (name = ANY($2::text[]) OR name LIKE '%[mktg]%')",
        T,
        broadcast_names,
    )
    await c.execute("DELETE FROM tenant_templates WHERE tenant_id=$1 AND name = ANY($2::text[])", T, names)

    tmpl_ids = {}
    for (name, cat, body) in TEMPLATES:
        comps = json.dumps([{"type": "BODY", "text": body}])
        tid = await c.fetchval(
            """INSERT INTO tenant_templates (tenant_id, tenant_channel_id, name, language, category, status, components, last_status_change_at, submitted_at)
               VALUES ($1,$2,$3,'en',$4,'approved',$5::jsonb,$6,$6) RETURNING id""",
            T, wa, name, cat, comps, NOW - timedelta(days=5),
        )
        tmpl_ids[name] = tid
    print("templates:", len(tmpl_ids))

    broadcasts = [
        ("June Promo Blast", "monthly_promo", "completed", 420, 420, 405, 312, NOW - timedelta(days=2)),
        ("Appointment Reminders", "appointment_reminder", "scheduled", 85, 0, 0, 0, NOW + timedelta(days=1)),
    ]
    for (bname, tname, status, total, sent, delivered, read, when) in broadcasts:
        await c.execute(
            # audience_filter MUST carry a valid filter_kind — an empty '{}'
            # fails the BroadcastDTO discriminated union and 500s the list API.
            """INSERT INTO broadcasts (tenant_id, tenant_channel_id, template_id, name, audience_filter, status,
                   scheduled_at, started_at, finished_at, total_recipients, sent_count, delivered_count, read_count)
               VALUES ($1,$2,$3,$4,'{"filter_kind":"all_opted_in"}'::jsonb,$5::broadcast_status,$6,$7,$8,$9,$10,$11,$12)""",
            T, wa, tmpl_ids[tname], bname, status,
            when,
            (when if status == "completed" else None),
            (when if status == "completed" else None),
            total, sent, delivered, read,
        )
    print("broadcasts: 2")
    await c.close()


asyncio.run(main())
