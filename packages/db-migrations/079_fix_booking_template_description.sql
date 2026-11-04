-- Migration 079: Remove Easy!Appointments branding from the booking flow
-- template description. LEAK_RE in the Phase 20 E2E spec flags any text
-- matching /easy\s*!?\s*appointments/ as a whitelabel violation.
-- The description was seeded by migration 038 and must be patched here
-- since migrations are append-only.

UPDATE flow_templates
   SET description     = 'Detects booking intent and hands off to the bookings team. Wire to your calendar system for appointment booking.',
       last_updated_at = now()
 WHERE slug = 'booking'
   AND description LIKE '%Easy!Appointments%';
