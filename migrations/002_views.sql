-- CityHostings materialised views for KPI aggregation
-- Run this after 001_schema.sql.
-- These views are refreshed at the end of every sync run.

-- A row per unit per night
drop materialized view if exists unit_nights cascade;
create materialized view unit_nights as
select
  r.property_id,
  r.unit_id,
  d::date                                as night,
  r.id                                   as reservation_id,
  r.source,
  r.net_amount     / nullif(r.nights, 0) as room_revenue_for_night,
  r.ota_commission / nullif(r.nights, 0) as ota_fee_for_night
from reservations r,
  generate_series(r.check_in, r.check_out - interval '1 day', interval '1 day') d
where r.status in ('confirmed', 'checked_out');

create index if not exists idx_unit_nights_property_night on unit_nights(property_id, night);
create index if not exists idx_unit_nights_night on unit_nights(night);

-- Per property per month KPIs
drop materialized view if exists monthly_kpis cascade;
create materialized view monthly_kpis as
with available as (
  -- Available room nights = active units × days in month
  select
    u.property_id,
    date_trunc('month', gs)::date as month,
    count(*) as available_room_nights
  from units u
  cross join generate_series(
    date_trunc('month', current_date - interval '36 months'),
    date_trunc('month', current_date + interval '1 month'),
    interval '1 day'
  ) gs
  where u.active = true
  group by u.property_id, date_trunc('month', gs)
)
select
  un.property_id,
  date_trunc('month', un.night)::date as month,
  count(distinct un.reservation_id)   as bookings,
  count(*)                            as room_nights_sold,
  coalesce(av.available_room_nights, 0) as available_room_nights,
  sum(un.room_revenue_for_night)      as room_revenue,
  sum(un.ota_fee_for_night)           as ota_fees,
  sum(un.room_revenue_for_night) / nullif(count(*), 0) as adr,
  count(*)::numeric / nullif(av.available_room_nights, 0) as occupancy_rate,
  sum(un.room_revenue_for_night) / nullif(av.available_room_nights, 0) as revpar
from unit_nights un
left join available av
  on av.property_id = un.property_id
  and av.month = date_trunc('month', un.night)::date
group by un.property_id, date_trunc('month', un.night), av.available_room_nights;

create unique index if not exists idx_monthly_kpis_property_month
  on monthly_kpis(property_id, month);

-- Source mix view (used by AI commentary and dashboard)
drop materialized view if exists monthly_source_mix cascade;
create materialized view monthly_source_mix as
select
  property_id,
  date_trunc('month', night)::date as month,
  coalesce(source, 'direct') as source,
  sum(room_revenue_for_night) as revenue,
  count(*) as nights
from unit_nights
group by property_id, date_trunc('month', night), coalesce(source, 'direct');

create index if not exists idx_source_mix on monthly_source_mix(property_id, month);

-- Helper function to refresh everything (called from Python at end of each sync)
create or replace function refresh_all_kpis() returns void as $$
begin
  refresh materialized view concurrently unit_nights;
  refresh materialized view concurrently monthly_kpis;
  refresh materialized view monthly_source_mix;
end;
$$ language plpgsql;
