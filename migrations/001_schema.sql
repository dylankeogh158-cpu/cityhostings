-- CityHostings core schema
-- Run this once in Supabase SQL Editor.
-- Idempotent: safe to run multiple times.

create extension if not exists "uuid-ossp";

-- 1. Properties owned/managed by CityHostings
create table if not exists properties (
  id              uuid primary key default uuid_generate_v4(),
  cloudbeds_id    text unique not null,
  name            text not null,
  location        text,
  owner_name      text,
  owner_email     text not null,
  currency        text default 'GBP',
  mgmt_fee_pct    numeric(5,2) default 20.0,
  active          boolean default true,
  created_at      timestamptz default now()
);

-- 2. Individual units (rooms/listings) under a property
create table if not exists units (
  id                uuid primary key default uuid_generate_v4(),
  property_id       uuid references properties(id) on delete cascade,
  cloudbeds_room_id text unique not null,
  name              text not null,
  max_occupancy     int,
  active            boolean default true
);

-- 3. Reservations
create table if not exists reservations (
  id                    uuid primary key default uuid_generate_v4(),
  cloudbeds_id          text unique not null,
  property_id           uuid references properties(id) on delete cascade,
  unit_id               uuid references units(id) on delete set null,
  source                text,
  status                text,
  check_in              date not null,
  check_out             date not null,
  nights                int generated always as (check_out - check_in) stored,
  guest_name            text,
  gross_amount          numeric(12,2),
  net_amount            numeric(12,2),
  ota_commission        numeric(12,2) default 0,
  cleaning_fee_charged  numeric(12,2) default 0,
  currency              text,
  booked_at             timestamptz,
  modified_at           timestamptz,
  cloudbeds_payload     jsonb,
  synced_at             timestamptz default now()
);

create index if not exists idx_reservations_property_check_in
  on reservations(property_id, check_in);
create index if not exists idx_reservations_modified_at
  on reservations(modified_at);
create index if not exists idx_reservations_status
  on reservations(status);

-- 4. Monthly expenses (manual entry)
create table if not exists expenses (
  id          uuid primary key default uuid_generate_v4(),
  property_id uuid references properties(id) on delete cascade,
  month       date not null,
  category    text not null check (category in ('cleaning','linen','maintenance','utilities','other')),
  amount      numeric(12,2) not null,
  note        text,
  created_by  text,
  created_at  timestamptz default now()
);

create index if not exists idx_expenses_property_month
  on expenses(property_id, month);

-- 5. Availability snapshots (optional; only used if you want strict occupancy)
create table if not exists unit_availability (
  date            date not null,
  unit_id         uuid references units(id) on delete cascade,
  is_available    boolean not null,
  primary key (date, unit_id)
);

-- 6. Generated monthly reports (audit + re-send)
create table if not exists monthly_reports (
  id              uuid primary key default uuid_generate_v4(),
  property_id     uuid references properties(id) on delete cascade,
  month           date not null,
  pdf_url         text,
  ai_commentary   text,
  kpis_snapshot   jsonb,
  emailed_at      timestamptz,
  emailed_to      text,
  unique (property_id, month)
);

-- 7. Sync run log
create table if not exists sync_runs (
  id                uuid primary key default uuid_generate_v4(),
  started_at        timestamptz default now(),
  finished_at       timestamptz,
  status            text default 'running',
  records_upserted  int default 0,
  error_message     text
);

-- Auth profile table for the dashboard (maps Supabase auth users to properties they can see)
create table if not exists owner_access (
  user_id      uuid primary key,        -- references auth.users(id)
  property_id  uuid references properties(id) on delete cascade,
  is_admin     boolean default false,
  created_at   timestamptz default now()
);
