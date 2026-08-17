-- New contacts table for Apollo-enriched decision makers.
-- Confirm companies.id is uuid before applying. If it is not, change
-- contacts.company_id to match, then re-add the foreign key.

CREATE TABLE IF NOT EXISTS contacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
  apollo_id text NOT NULL UNIQUE,
  first_name text,
  last_name text,
  full_name text,
  title text,
  seniority text,
  linkedin_url text,
  email text,
  phone text,
  phone_reveal_request_id text,
  apollo_contact_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS contacts_company_id_idx
  ON contacts (company_id);

CREATE OR REPLACE FUNCTION set_contacts_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS contacts_set_updated_at ON contacts;
CREATE TRIGGER contacts_set_updated_at
  BEFORE UPDATE ON contacts
  FOR EACH ROW
  EXECUTE PROCEDURE set_contacts_updated_at();
