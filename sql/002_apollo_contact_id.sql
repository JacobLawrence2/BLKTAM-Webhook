-- Optional follow-up if 001_contacts.sql was already applied.
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS apollo_contact_id text;
