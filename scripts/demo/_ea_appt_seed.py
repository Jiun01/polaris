# Emits SQL to fill the provider-polaris (tenant 0000..0001) EA calendar with a
# believable week of appointments. Idempotent: notes tagged [mktg], deleted first.
import hashlib

T = "00000000-0000-0000-0000-000000000001"
PROV = 100
SVC_30, SVC_60 = 69, 70  # service ids for this tenant
COLORS = ["#F76F53", "#6287F5", "#3D9A5F", "#7cbae8", "#D9943B"]
NAMES = [
    ("Aisyah", "Rahman"), ("Wei Jie", "Lim"), ("Daniel", "Tan"), ("Nurul", "Huda"),
    ("Farah", "Idris"), ("Siti", "Mariam"), ("Jason", "Lim"), ("Mei Ling", "Chong"),
    ("Arjun", "Kumar"), ("Hafiz", "Azlan"), ("Chloe", "Wong"), ("Imran", "Yusof"),
]

# Current week Mon-Fri (Jun 1-5 2026). (date, [(start_hhmm, dur_min), ...])
WEEK = [
    ("2026-06-01", [("09:00", 60), ("10:30", 30), ("11:30", 60), ("14:00", 30), ("15:00", 60), ("16:30", 30)]),
    ("2026-06-02", [("09:30", 30), ("10:30", 60), ("13:00", 60), ("14:30", 30), ("16:00", 60)]),
    ("2026-06-03", [("09:00", 30), ("10:00", 60), ("11:30", 30), ("14:00", 60), ("15:30", 30), ("16:30", 60)]),
    ("2026-06-04", [("09:00", 60), ("10:30", 60), ("13:30", 30), ("14:30", 60), ("16:00", 30)]),
    ("2026-06-05", [("09:30", 60), ("11:00", 30), ("13:00", 60), ("15:00", 30), ("16:00", 60)]),
]

# --- cleanup + fresh demo customers (don't touch E2E fixtures) ---
print(f"DELETE FROM ea_appointments WHERE tenant_id='{T}' AND notes LIKE '%[mktg]%';")
print(f"DELETE FROM ea_users WHERE tenant_id='{T}' AND email LIKE 'mktg+%@demo.local';")
print("SET @CUST_ROLE = (SELECT id FROM ea_roles WHERE slug='customer');")
cust_vars = []
for n, (fn, ln) in enumerate(NAMES, 1):
    print(
        "INSERT INTO ea_users (tenant_id, first_name, last_name, email, phone_number, address, city, "
        "zip_code, notes, is_private, id_roles, language, timezone, create_datetime, update_datetime) VALUES "
        f"('{T}', '{fn}', '{ln}', 'mktg+{n}@demo.local', '', '', '', '', '', 0, @CUST_ROLE, 'english', "
        "'Asia/Kuala_Lumpur', NOW(), NOW());"
    )
    print(f"SET @c{n} = LAST_INSERT_ID();")
    cust_vars.append(f"@c{n}")

i = 0
for date, slots in WEEK:
    for (hhmm, dur) in slots:
        svc = SVC_60 if dur == 60 else SVC_30
        cust = cust_vars[i % len(cust_vars)]
        color = COLORS[i % len(COLORS)]
        sh, sm = hhmm.split(":")
        start = f"{date} {sh}:{sm}:00"
        em = int(sm) + dur
        eh = int(sh) + em // 60
        em = em % 60
        end = f"{date} {eh:02d}:{em:02d}:00"
        h = hashlib.md5(f"{start}{cust}{svc}".encode()).hexdigest()
        notes = "[mktg] booked via Polaris AI"
        print(
            "INSERT INTO ea_appointments (tenant_id, create_datetime, update_datetime, book_datetime, "
            "start_datetime, end_datetime, notes, hash, color, status, is_unavailability, "
            "id_users_provider, id_users_customer, id_services) VALUES "
            f"('{T}', NOW(), NOW(), NOW(), '{start}', '{end}', '{notes}', '{h}', '{color}', '', 0, "
            f"{PROV}, {cust}, {svc});"
        )
        i += 1
print(f"SELECT count(*) AS seeded FROM ea_appointments WHERE tenant_id='{T}' AND notes LIKE '%[mktg]%';")
