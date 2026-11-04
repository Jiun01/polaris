"""Marketing-screenshot seed for the polaris-owner (Polaris HQ) dogfood tenant.

Idempotent: every row it writes is tagged (conversations.metadata.seed='mktg',
flow names prefixed, ingestion source_url marked, promotions title marked) and
deleted on re-run. SAFE to run repeatedly; SAFE to fully remove by deleting the tagged rows.

Content is generic Malaysian F&B/SME customer service — NO real client data.
Customer names are fake first-name + initial. Phone numbers are fake.
"""
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import asyncpg

TENANT = "00000000-0000-0000-0000-000000000001"  # polaris-owner / Polaris HQ
NOW = datetime.now(UTC)


def ago(days=0, hours=0, minutes=0):
    return NOW - timedelta(days=days, hours=hours, minutes=minutes)


# (channel, display_name, phone, [(role, content, intent, model, rt_ms), ...], day_offset)
SHOWCASE = [
    ("whatsapp", "Aisyah R.", "60128840012", [
        ("user", "Hi, are you open today?", None, None, None),
        ("assistant", "Hi Aisyah! Yes, we're open today 9am to 6pm 😊 Would you like me to book you an appointment?", "faq", "haiku", 412),
        ("user", "maybe later, thanks!", None, None, None),
    ], 0),
    ("whatsapp", "Wei Jie", "60173320988", [
        ("user", "boleh share your pricing?", None, None, None),
        ("assistant", "Of course! The first consultation is free (30 min), and a full session is RM250 (1 hour). Would you like me to book you a slot?", "pricing", "haiku", 530),
        ("user", "yes please", None, None, None),
        ("assistant", "Great! 🙂 I have openings this Thursday and Friday afternoon. Which works better for you?", "pricing", "sonnet", 1180),
    ], 0),
    ("whatsapp", "Daniel Tan", "60192245571", [
        ("user", "I'd like to book a consultation this Saturday 3pm", None, None, None),
        ("assistant", "Got it Daniel, a 30-min Consultation this Saturday (7 Jun) at 3:00pm. Shall I confirm the booking?", "booking", "haiku", 470),
        ("user", "yes confirm please", None, None, None),
        ("assistant", "✅ Booked! Consultation, Sat 7 Jun, 3:00pm under Daniel. You'll get a reminder 2 hours before. See you then!", "booking", "haiku", 505),
    ], 1),
    ("instagram", "Nurul Huda", "ig_nurul.h", [
        ("user", "do you offer online sessions?", None, None, None),
        ("assistant", "Yes! 💻 We offer both online and in-person sessions. Want me to book you one?", "product", "haiku", 398),
        ("user", "yes, online tomorrow 3pm 🙏", None, None, None),
        ("assistant", "Done! Online session booked for tomorrow 3:00pm. We'll send you the link ✨", "booking", "haiku", 441),
    ], 1),
    ("whatsapp", "Farah", "60134567720", [
        ("user", "where are you located?", None, None, None),
        ("assistant", "We're at Level 12, Menara Maxis, KLCC 📍 We also offer online sessions. Anything else I can help you with?", "logistics", "haiku", 388),
    ], 2),
    ("messenger", "Kavin R.", "fb_kavin.raj", [
        ("user", "Hi, I'd like to arrange sessions for my whole team, can someone help?", None, None, None),
        ("assistant", "Of course 🙏 For team bookings I'll connect you with our team. Someone will reach out shortly to sort out the details.", "support", "sonnet", 1320),
    ], 2),
    ("whatsapp", "Siti M.", "60198812204", [
        ("user", "any promo this month?", None, None, None),
        ("assistant", "Yes! 🎉 This month: a free 30-min consultation with every full session booked. Want me to book you in?", "promo", "haiku", 462),
        ("user", "ok book for tomorrow", None, None, None),
        ("assistant", "You got it 😊 Session booked for tomorrow, with your free consultation included. See you then, Siti!", "promo", "haiku", 503),
    ], 3),
    ("whatsapp", "Jason Lim", "60165539071", [
        ("user", "can I move my appointment tonight to 8pm instead of 6.30?", None, None, None),
        ("assistant", "Sure Jason! ✅ I've moved your appointment from 6:30pm to 8:00pm tonight. Anything else?", "booking", "haiku", 449),
        ("user", "perfect thank you", None, None, None),
    ], 3),
]

# Lightweight extra conversations purely for list volume + 7-day chart bars.
FILLER_LINES = [
    ("whatsapp", "where are you located?", "We're at Level 12, Menara Maxis, KLCC 📍 Online sessions are available too."),
    ("whatsapp", "do you offer online consultations?", "Yes! 💻 Online consultations are available daily. Shall I book you a slot?"),
    ("whatsapp", "how long is a session?", "Consultations are 30 minutes and full sessions are 1 hour. Want me to book you in?"),
    ("instagram", "are you open this weekend?", "Yes, we're open Saturday 9am to 1pm 😊 Want me to book you an appointment?"),
    ("whatsapp", "is parking available?", "Yes, free parking is available at the building 🅿️"),
    ("whatsapp", "can I book for this Saturday?", "Of course! 🙂 I have a few slots open this Saturday. What time works for you?"),
    ("messenger", "do you take card payment?", "Yes! Card, DuitNow QR and e-wallets are all accepted 💳"),
    ("whatsapp", "how much is a full session?", "A full 1-hour session is RM250 💼 The first 30-min consultation is free. Want me to book you in?"),
    ("whatsapp", "are you open now?", "Yes, we're open till 6pm today 😊 Want me to book you an appointment?"),
    ("instagram", "do I need to prepare anything?", "Nothing at all 🙂 Just bring your questions. Shall I book you a slot?"),
]


async def main():
    c = await asyncpg.connect(os.environ["POLARIS_DB_URL_SERVICE"])

    # --- clean prior seed ---
    await c.execute(
        "DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE tenant_id=$1 AND metadata->>'seed'='mktg')",
        TENANT,
    )
    await c.execute("DELETE FROM conversations WHERE tenant_id=$1 AND metadata->>'seed'='mktg'", TENANT)
    await c.execute(
        "DELETE FROM ingestion_requests WHERE tenant_id=$1 AND (source_url LIKE '%[mktg]%' OR source_text LIKE '%[mktg]%' OR array_to_string(storage_paths,',') LIKE '%[mktg]%')",
        TENANT,
    )
    await c.execute("DELETE FROM tenant_promotions WHERE tenant_id=$1 AND (title LIKE '%[mktg]%' OR terms LIKE '%[mktg]%')", TENANT)
    await c.execute("UPDATE flow_definitions SET current_version_id=NULL WHERE tenant_id=$1 AND name LIKE 'MKTG %'", TENANT)
    await c.execute(
        "DELETE FROM flow_versions WHERE tenant_id=$1 AND flow_definition_id IN (SELECT id FROM flow_definitions WHERE tenant_id=$1 AND name LIKE 'MKTG %')",
        TENANT,
    )
    await c.execute("DELETE FROM flow_definitions WHERE tenant_id=$1 AND name LIKE 'MKTG %'", TENANT)

    async def add_conversation(channel, name, contact, turns, day_offset, handoff=False):
        base = ago(days=day_offset, hours=2)
        last = base + timedelta(minutes=max(1, (len(turns) - 1)) * 3)
        meta = {"seed": "mktg", "display_name": name}
        conv_id = await c.fetchval(
            """INSERT INTO conversations (tenant_id, channel, channel_user_id, last_message_at, metadata, created_at, handoff_at)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7) RETURNING id""",
            TENANT, channel, contact, last, json.dumps(meta), base,
            (last if handoff else None),
        )
        allowed = {"greeting", "faq", "booking", "handoff", "promo", "other"}
        t = base
        for (role, content, intent, model, rt) in turns:
            if intent is not None and intent not in allowed:
                intent = "other"
            await c.execute(
                """INSERT INTO messages (tenant_id, conversation_id, role, content, intent, model_used, response_time_ms,
                       input_tokens, output_tokens, metadata, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'{}'::jsonb,$10)""",
                TENANT, conv_id, role, content, intent, model, rt,
                (180 if role == "assistant" else None),
                (60 if role == "assistant" else None),
                t,
            )
            t += timedelta(minutes=3)
        return conv_id

    # showcase
    for (channel, name, contact, turns, day) in SHOWCASE:
        await add_conversation(channel, name, contact, turns, day, handoff=(name == "Kavin R."))

    # filler convos spread across last 7 days for chart volume
    fnames = ["Hafiz", "Mei Ling", "Arjun", "Chloe", "Imran", "Yusof", "Pavithra", "Brandon", "Suri", "Zara"]
    for i, (channel, q, a) in enumerate(FILLER_LINES):
        day = i % 7
        await add_conversation(
            channel, f"{fnames[i]} {chr(65 + i)}.", f"60{17000000 + i*131:08d}",
            [("user", q, None, None, None), ("assistant", a, "faq", "haiku", 400 + i * 7)],
            day,
        )

    # extra assistant messages to give the 7-day chart fuller bars (attached to showcase convos)
    conv_ids = [r["id"] for r in await c.fetch(
        "SELECT id FROM conversations WHERE tenant_id=$1 AND metadata->>'seed'='mktg' ORDER BY created_at LIMIT 8", TENANT)]
    extras = [
        "Noted! 😊", "Sure, happy to help!", "Done ✅", "You're welcome!", "Anytime! 🙏",
        "Confirmed for you.", "Great choice!", "See you soon! 👋", "All set 👍", "No problem at all!",
    ]
    for d in range(7):
        for k in range((6 - d) + 4):  # more recent days slightly busier
            conv = conv_ids[(d + k) % len(conv_ids)]
            await c.execute(
                """INSERT INTO messages (tenant_id, conversation_id, role, content, intent, model_used, response_time_ms, metadata, created_at)
                   VALUES ($1,$2,'assistant',$3,'faq','haiku',$4,'{}'::jsonb,$5)""",
                TENANT, conv, extras[k % len(extras)], 380 + k * 5,
                ago(days=d, hours=3, minutes=k * 7),
            )

    # --- knowledge sources (ingestion_requests, completed) ---
    # (source_type, url, text, storage_paths, chunks, tokens)
    sources = [
        ("url", "https://demo.cafe.my/menu  [mktg]", None, None, 64, 9120),
        ("url", "https://demo.cafe.my/faq  [mktg]", None, None, 28, 3870),
        ("text", None, "Opening hours, delivery zones & catering FAQ  [mktg]", None, 19, 2110),
        ("file", None, None, ["tenant/demo/menu-specials-june.pdf  [mktg]"], 41, 5230),
    ]
    for (stype, url, text, paths, chunks, toks) in sources:
        await c.execute(
            """INSERT INTO ingestion_requests (tenant_id, source_type, source_url, source_text, storage_paths, ingest_mode, status,
                   submitted_at, started_at, completed_at, chunks_added, chunks_skipped, chunks_deleted, tokens_used, priority)
               VALUES ($1,$2::ingestion_source_type,$3,$4,$5,'sync'::ingestion_mode,'completed'::ingestion_status,
                   $6,$6,$7,$8,0,0,$9,'normal'::ingestion_priority)""",
            TENANT, stype, url, text, paths, ago(days=3), ago(days=3, hours=-1), chunks, toks,
        )

    # --- promotions (active) ---
    promos = [
        ("Set Lunch 20% Off", "20% off all set lunches, dine-in & pickup.", "20% OFF", "Mon–Fri, 12–3pm. Not valid with other promos. [mktg]"),
        ("Gula Melaka Latte B1F1", "Buy 1 Free 1 on our signature Gula Melaka Latte.", "Buy 1 Free 1", "Whole week. Dine-in only. [mktg]"),
    ]
    for (title, desc, disc, terms) in promos:
        await c.execute(
            """INSERT INTO tenant_promotions (tenant_id, title, description, discount_text, terms, valid_from, valid_until, is_active)
               VALUES ($1,$2,$3,$4,$5,$6,$7,true)""",
            TENANT, title, desc, disc, terms, ago(days=2), ago(days=-12),
        )

    # --- dashboard quota dials ---
    await c.execute(
        """UPDATE tenant_quotas SET
              bot_replies_used_this_period = 1847,
              knowledge_chunks_total_mb_used = 18,
              broadcasts_sent_this_period = 3,
              bookings_made_this_period = 27
           WHERE tenant_id=$1""",
        TENANT,
    )
    await c.execute("UPDATE tenants SET last_message_at=$2 WHERE id=$1", TENANT, NOW)

    # --- clone two templates into real flows (for the canvas + flows list) ---
    tmpls = await c.fetch(
        "SELECT slug, name, definition FROM flow_templates WHERE slug IN ('welcome-faq','booking') OR name ILIKE '%booking%' OR slug ILIKE '%welcome%'")
    chosen = {}
    for r in tmpls:
        chosen[r["slug"]] = r
    pick = list(chosen.values())[:2] if chosen else []
    flow_names = {"welcome-faq": "MKTG Welcome + FAQ", "booking": "MKTG Booking + Handoff"}
    for r in pick:
        defn = r["definition"]
        defn_json = defn if isinstance(defn, str) else json.dumps(defn)
        name = flow_names.get(r["slug"], f"MKTG {r['name']}")
        fid = await c.fetchval(
            """INSERT INTO flow_definitions (tenant_id, name, description, draft)
               VALUES ($1,$2,$3,$4::jsonb) RETURNING id""",
            TENANT, name, r["name"], defn_json,
        )
        vid = await c.fetchval(
            """INSERT INTO flow_versions (flow_definition_id, tenant_id, definition)
               VALUES ($1,$2,$3::jsonb) RETURNING id""",
            fid, TENANT, defn_json,
        )
        await c.execute("UPDATE flow_definitions SET current_version_id=$2 WHERE id=$1", fid, vid)

    # report
    conv_n = await c.fetchval("SELECT count(*) FROM conversations WHERE tenant_id=$1 AND metadata->>'seed'='mktg'", TENANT)
    msg_n = await c.fetchval("SELECT count(*) FROM messages WHERE tenant_id=$1 AND metadata->>'seed' IS NULL AND conversation_id IN (SELECT id FROM conversations WHERE tenant_id=$1 AND metadata->>'seed'='mktg')", TENANT)
    flow_n = await c.fetchval("SELECT count(*) FROM flow_definitions WHERE tenant_id=$1 AND name LIKE 'MKTG %'", TENANT)
    print(f"seeded: conversations={conv_n}, messages={msg_n}, flows={flow_n}")
    await c.close()


asyncio.run(main())
