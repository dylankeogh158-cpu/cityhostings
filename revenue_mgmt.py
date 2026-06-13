"""CityHostings — Internal Revenue Management Dashboard.

Single-page Streamlit app for the management team.

NOT for owners. Owners get the monthly PDF — this is the back-office view.

Deploy to share.streamlit.io. Set secrets in App Settings → Secrets:
    DATABASE_URL = "postgresql://..."  (same as GitHub Actions)
    DASHBOARD_PASSWORD = "your-team-password"
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row


# ============================================================================
# Config
# ============================================================================

OTA_RATES = {
    'booking.com': 0.15,
    'expedia': 0.15,
    'airbnb': 0.15,
    'airbnb (api)': 0.15,
    'vrbo': 0.08,
    'agoda': 0.15,
}

DIRECT_SOURCES = {'direct', 'walk-in', 'phone', 'email'}

st.set_page_config(
    page_title="CityHostings · Revenue Management",
    layout="wide",
    page_icon="📊",
    initial_sidebar_state="expanded",
)


def get_secret(key: str, default=None):
    return st.secrets.get(key) or os.environ.get(key) or default


DASHBOARD_PASSWORD = get_secret("DASHBOARD_PASSWORD")
DATABASE_URL = get_secret("DATABASE_URL")

if not DATABASE_URL:
    st.error(
        "Missing DATABASE_URL secret. Configure it in Streamlit Cloud "
        "→ App settings → Secrets."
    )
    st.stop()


# ============================================================================
# Auth (single shared password for the team)
# ============================================================================

def auth_gate() -> bool:
    if st.session_state.get("authed"):
        return True

    st.markdown(
        "<div style='text-align:center; padding-top:80px'>"
        "<h1 style='color:#1F3A5F'>🏢 CityHostings</h1>"
        "<h3 style='color:#666; font-weight:400'>Revenue Management Tool</h3>"
        "<p style='color:#999; font-size:14px'>Internal use only</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    cols = st.columns([1, 2, 1])
    with cols[1]:
        with st.form("login"):
            pw = st.text_input("Team password", type="password", label_visibility="collapsed", placeholder="Team password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
            if submitted:
                if pw == DASHBOARD_PASSWORD:
                    st.session_state.authed = True
                    st.rerun()
                else:
                    st.error("Wrong password")
    return False


if not auth_gate():
    st.stop()


# ============================================================================
# DB helpers
# ============================================================================

@st.cache_resource
def get_db():
   return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None, autocommit=True)

def q(sql: str, params: tuple = ()) -> list[dict]:
    with get_db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@st.cache_data(ttl=300)
def df(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.DataFrame(q(sql, params))


# ============================================================================
# Sidebar
# ============================================================================

with st.sidebar:
    st.markdown("# 🏢 CityHostings")
    st.caption("Revenue Management")
    st.markdown("---")

    page = st.radio(
        "Navigate",
        ["📊 Portfolio Command Centre",
         "🎯 Pricing Opportunities",
         "📈 Direct vs OTA",
         "📉 Performance Analytics",
         "⚙️ Settings"],
        label_visibility="collapsed",
    )

    st.markdown("---")
    st.caption(f"Loaded at: {datetime.now().strftime('%H:%M')}")
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()

    if st.button("Sign out", use_container_width=True):
        st.session_state.authed = False
        st.rerun()


# ============================================================================
# Page: Portfolio Command Centre
# ============================================================================

def page_portfolio():
    st.title("📊 Portfolio Command Centre")
    st.caption(f"Live as of {date.today().strftime('%A, %B %d, %Y')}")

    today = date.today()
    month_start = today.replace(day=1)
    days_elapsed = (today - month_start).days + 1
    days_in_month = ((month_start + relativedelta(months=1)) - month_start).days
    next_7 = today + timedelta(days=7)
    next_30 = today + timedelta(days=30)
    next_90 = today + timedelta(days=90)

    # ---- Headline KPIs ----
    rev_mtd = float(q("""
        select coalesce(sum(net_amount), 0) as total
        from reservations
        where check_in >= %s and check_in <= %s
          and status in ('confirmed', 'checked_out')
    """, (month_start, today))[0]['total'])

    bookings_mtd = q("""
        select count(*) as total
        from reservations
        where check_in >= %s and check_in <= %s
          and status in ('confirmed', 'checked_out')
    """, (month_start, today))[0]['total']

    pace_forecast = (rev_mtd / days_elapsed) * days_in_month if days_elapsed > 0 else 0

    ota_rev = float(q("""
        select coalesce(sum(net_amount), 0) as total
        from reservations
        where check_in >= %s and check_in <= %s
          and status in ('confirmed', 'checked_out')
          and lower(coalesce(source,'')) in ('booking.com','expedia','airbnb','airbnb (api)','agoda')
    """, (month_start, today))[0]['total'])
    lost_ota = ota_rev * 0.15

    future_rev_90 = float(q("""
        select coalesce(sum(net_amount), 0) as total
        from reservations
        where check_in > %s and check_in <= %s
          and status in ('confirmed', 'checked_out')
    """, (today, next_90))[0]['total'])

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Revenue MTD", f"€{rev_mtd:,.0f}")
    c2.metric("Bookings MTD", f"{bookings_mtd}")
    c3.metric("Forecast (this mo.)", f"€{pace_forecast:,.0f}",
              help="Linear projection: current MTD revenue × days in month / days elapsed")
    c4.metric("Lost to OTA fees", f"€{lost_ota:,.0f}", delta_color="inverse",
              help="Estimated 15% commission on OTA bookings this month")
    c5.metric("Booked next 90d", f"€{future_rev_90:,.0f}",
              help="Confirmed revenue with check-in dates in the next 90 days")

    st.markdown("---")

    # ---- Alerts ----
    st.subheader("⚠️ Alerts — needs your attention")

    alerts = []
    properties = q("select id, name, total_units from properties where active = true")

    for prop in properties:
        sold = q("""
            select count(*) as nights
            from reservations r
            cross join generate_series(r.check_in, r.check_out - interval '1 day', '1 day') gs
            where r.property_id = %s
              and r.status in ('confirmed', 'checked_out')
              and gs::date >= %s and gs::date <= %s
        """, (prop['id'], today, next_7))[0]['nights']

        available = (prop['total_units'] or 1) * 7
        if available > 0:
            unsold_pct = 100 * (available - sold) / available
            if unsold_pct > 60:
                alerts.append(("🔴", prop['name'],
                               f"{int(available - sold)} unsold nights in next 7 days ({unsold_pct:.0f}% empty)"))
            elif unsold_pct > 40:
                alerts.append(("🟡", prop['name'],
                               f"{int(available - sold)} unsold nights in next 7 days ({unsold_pct:.0f}% empty)"))

        direct_row = q("""
            select
              coalesce(sum(case when lower(coalesce(source,'direct')) in ('direct','walk-in') then net_amount else 0 end), 0) as direct,
              coalesce(sum(net_amount), 0) as total
            from reservations
            where property_id = %s
              and check_in >= %s and check_in <= %s
              and status in ('confirmed', 'checked_out')
        """, (prop['id'], today - timedelta(days=30), today))[0]
        if direct_row['total'] and float(direct_row['total']) > 0:
            direct_pct = 100 * float(direct_row['direct']) / float(direct_row['total'])
            if direct_pct < 5:
                alerts.append(("🟡", prop['name'],
                               f"Direct bookings only {direct_pct:.1f}% of revenue — heavy OTA dependence"))

    if alerts:
        for level, name, msg in alerts:
            st.markdown(f"{level} **{name}** — {msg}")
    else:
        st.success("✅ All properties trending healthy")

    st.markdown("---")

    # ---- Per-property cards ----
    st.subheader("Properties")

    for prop in properties:
        with st.container(border=True):
            this_mtd = q("""
                select coalesce(sum(net_amount), 0) as total, count(*) as n
                from reservations
                where property_id = %s and check_in >= %s and check_in <= %s
                  and status in ('confirmed', 'checked_out')
            """, (prop['id'], month_start, today))[0]

            future = q("""
                select coalesce(sum(net_amount), 0) as total, count(*) as n
                from reservations
                where property_id = %s and check_in > %s and check_in <= %s
                  and status in ('confirmed', 'checked_out')
            """, (prop['id'], today, next_30))[0]

            cols = st.columns([2, 1, 1, 1, 1])
            cols[0].markdown(f"### 🏠 {prop['name']}")
            cols[0].caption(f"{prop['total_units']} units")
            cols[1].metric("Revenue MTD", f"€{float(this_mtd['total']):,.0f}")
            cols[2].metric("Bookings MTD", f"{this_mtd['n']}")
            cols[3].metric("Next 30d revenue", f"€{float(future['total']):,.0f}")
            cols[4].metric("Next 30d bookings", f"{future['n']}")


# ============================================================================
# Page: Direct vs OTA Analysis
# ============================================================================

def page_direct_vs_ota():
    st.title("📈 Direct vs OTA Analysis")
    st.caption("Channel mix, lost margin, and direct-booking opportunities")

    days_back = st.slider("Look back (days)", 30, 365, 90)
    cutoff = date.today() - timedelta(days=days_back)

    mix = df("""
        select
          coalesce(source, 'direct') as source,
          count(*) as bookings,
          coalesce(sum(net_amount), 0) as revenue
        from reservations
        where check_in >= %s
          and status in ('confirmed', 'checked_out')
        group by coalesce(source, 'direct')
        order by revenue desc
    """, (cutoff,))

    if mix.empty:
        st.info("No data for this period")
        return

    mix['revenue'] = mix['revenue'].astype(float)
    total_rev = mix['revenue'].sum()
    if total_rev == 0:
        st.info("No revenue in this period")
        return

    mix['share_pct'] = (mix['revenue'] / total_rev * 100).round(1)
    mix['fee_rate'] = mix['source'].apply(lambda s: OTA_RATES.get(str(s).strip().lower(), 0))
    mix['fee_paid'] = (mix['revenue'] * mix['fee_rate']).round(2)

    total_fees = mix['fee_paid'].sum()
    total_direct = mix[mix['source'].str.lower().isin(DIRECT_SOURCES)]['revenue'].sum()
    direct_share = (total_direct / total_rev * 100) if total_rev else 0

    saved_at_25pct = max(0, (0.25 - direct_share / 100) * total_rev * 0.15)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total revenue", f"€{total_rev:,.0f}")
    c2.metric("OTA fees paid", f"€{total_fees:,.0f}", delta_color="inverse")
    c3.metric("Direct share", f"{direct_share:.1f}%")
    c4.metric("If direct → 25%", f"€{saved_at_25pct:+,.0f}",
              help="Estimated fee savings if direct share reached 25% (industry healthy benchmark)")

    st.markdown("---")
    st.subheader("Channel breakdown")

    display = mix[['source', 'bookings', 'revenue', 'share_pct', 'fee_paid']].copy()
    display.columns = ['Source', 'Bookings', 'Revenue', 'Share %', 'OTA fees']
    display['Revenue'] = display['Revenue'].apply(lambda x: f"€{x:,.0f}")
    display['OTA fees'] = display['OTA fees'].apply(lambda x: f"€{x:,.0f}")
    display['Share %'] = display['Share %'].apply(lambda x: f"{x}%")
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Repeat OTA guests — candidates for direct re-booking")

    repeat_guests = df("""
        select trim(guest_name) as guest, source,
               count(*) as stays,
               max(check_in) as last_stay,
               coalesce(sum(net_amount), 0) as total_spent
        from reservations
        where status in ('confirmed', 'checked_out')
          and guest_name is not null
          and length(trim(coalesce(guest_name, ''))) > 1
          and lower(coalesce(source,'direct')) not in ('direct','walk-in')
        group by trim(guest_name), source
        having count(*) >= 2
        order by stays desc, last_stay desc
        limit 50
    """)

    if not repeat_guests.empty:
        repeat_guests['total_spent'] = repeat_guests['total_spent'].astype(float)
        repeat_guests['fee_paid'] = (repeat_guests['total_spent'] * 0.15).round(2)
        st.dataframe(
            repeat_guests.rename(columns={
                'guest': 'Guest',
                'source': 'Booked via',
                'stays': 'Stays',
                'last_stay': 'Last stay',
                'total_spent': 'Total spent (€)',
                'fee_paid': 'You paid in fees (€)',
            }),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            f"💡 These {len(repeat_guests)} guests have stayed multiple times via OTAs. "
            f"Email them with a small discount to book direct next time — you'd save the OTA fee."
        )
    else:
        st.info("No repeat OTA guests found yet (need 2+ stays from same guest name).")


# ============================================================================
# Page: Performance Analytics
# ============================================================================

def page_performance():
    st.title("📉 Performance Analytics")
    st.caption("Trends, day-of-week patterns, lead time, source quality")

    st.subheader("13-month revenue trend (per property)")
    trend = df("""
        select
          p.name as property,
          mk.month,
          coalesce(mk.room_revenue, 0)::numeric as revenue
        from monthly_kpis mk
        join properties p on p.id = mk.property_id
        where mk.month >= date_trunc('month', current_date - interval '13 months')
          and mk.month <= date_trunc('month', current_date)
        order by mk.month, p.name
    """)

    if not trend.empty:
        trend['revenue'] = trend['revenue'].astype(float)
        pivot = trend.pivot(index='month', columns='property', values='revenue').fillna(0)
        st.line_chart(pivot)
    else:
        st.info("Not enough historical data yet")

    st.markdown("---")
    st.subheader("Day-of-week analysis (last 6 months)")

    dow = df("""
        select
          to_char(check_in, 'Day') as dow,
          extract(dow from check_in) as dow_num,
          count(*) as bookings,
          coalesce(avg(net_amount / nullif(nights, 0)), 0)::numeric as avg_rate
        from reservations
        where status in ('confirmed', 'checked_out')
          and check_in >= current_date - interval '6 months'
          and check_in <= current_date
        group by to_char(check_in, 'Day'), extract(dow from check_in)
        order by dow_num
    """)

    if not dow.empty:
        dow['avg_rate'] = dow['avg_rate'].astype(float).round(2)
        dow['dow'] = dow['dow'].str.strip()
        c1, c2 = st.columns(2)
        with c1:
            st.bar_chart(dow.set_index('dow')['bookings'])
            st.caption("Bookings by check-in day")
        with c2:
            st.bar_chart(dow.set_index('dow')['avg_rate'])
            st.caption("Avg nightly rate by check-in day")

    st.markdown("---")
    st.subheader("Lead time distribution")

    lead = df("""
        with categorised as (
          select
            case
              when extract(day from check_in - booked_at::date) < 0 then 0
              when extract(day from check_in - booked_at::date) <= 7 then 1
              when extract(day from check_in - booked_at::date) <= 30 then 2
              when extract(day from check_in - booked_at::date) <= 90 then 3
              else 4
            end as bucket,
            net_amount
          from reservations
          where status in ('confirmed', 'checked_out')
            and booked_at is not null
            and check_in >= current_date - interval '12 months'
        )
        select
          case bucket
            when 0 then 'Same day'
            when 1 then '1-7 days'
            when 2 then '8-30 days'
            when 3 then '31-90 days'
            else '90+ days'
          end as lead_time,
          bucket,
          count(*) as bookings,
          coalesce(avg(net_amount), 0)::numeric as avg_value
        from categorised
        group by bucket
        order by bucket
    """)

    if not lead.empty:
        lead['avg_value'] = lead['avg_value'].astype(float).round(2)
        st.dataframe(
            lead[['lead_time', 'bookings', 'avg_value']].rename(columns={
                'lead_time': 'Lead time',
                'bookings': 'Bookings',
                'avg_value': 'Avg booking value (€)',
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No lead-time data yet (need bookings with both check_in and booked_at)")

    st.markdown("---")
    st.subheader("Source quality (last 6 months)")

    source_quality = df("""
        select
          coalesce(source, 'direct') as source,
          count(*) as bookings,
          coalesce(avg(net_amount), 0)::numeric as avg_value,
          coalesce(avg(nights), 0)::numeric as avg_nights,
          sum(case when status = 'cancelled' then 1 else 0 end)::numeric / nullif(count(*), 0) * 100 as cancel_pct
        from reservations
        where check_in >= current_date - interval '6 months'
          and check_in <= current_date + interval '6 months'
        group by coalesce(source, 'direct')
        order by bookings desc
    """)

    if not source_quality.empty:
        source_quality['avg_value'] = source_quality['avg_value'].astype(float).round(2)
        source_quality['avg_nights'] = source_quality['avg_nights'].astype(float).round(2)
        source_quality['cancel_pct'] = source_quality['cancel_pct'].fillna(0).astype(float).round(1)
        st.dataframe(
            source_quality.rename(columns={
                'source': 'Source',
                'bookings': 'Bookings',
                'avg_value': 'Avg value (€)',
                'avg_nights': 'Avg nights',
                'cancel_pct': 'Cancel %',
            }),
            use_container_width=True,
            hide_index=True,
        )


def page_pricing():
    st.title("🎯 Pricing Opportunities")
    st.caption("Forward-looking dates needing rate adjustments — based on booking pace")

    today = date.today()
    horizon_days = st.slider("Look ahead (days)", 14, 90, 60)
    horizon = today + timedelta(days=horizon_days)

    properties = q("select id, name, total_units from properties where active = true order by name")

    rows = []
    for prop in properties:
        units = prop['total_units'] or 1

        # Baseline rate per day-of-week for this property (last 90 days)
        rates_by_dow = q("""
            select extract(dow from check_in) as dow,
                   avg(net_amount / nullif(nights, 0))::numeric as avg_rate
            from reservations
            where property_id = %s
              and status in ('confirmed', 'checked_out')
              and check_in >= current_date - interval '90 days'
              and net_amount > 0 and nights > 0
            group by extract(dow from check_in)
        """, (prop['id'],))
        rate_by_dow = {int(r['dow']): float(r['avg_rate'] or 0) for r in rates_by_dow}

        # Fallback overall avg
        overall = q("""
            select avg(net_amount / nullif(nights, 0))::numeric as avg_rate
            from reservations
            where property_id = %s
              and status in ('confirmed', 'checked_out')
              and check_in >= current_date - interval '90 days'
              and net_amount > 0 and nights > 0
        """, (prop['id'],))
        overall_rate = float(overall[0]['avg_rate'] or 0) if overall else 0

        daily = q("""
            select gs::date as night,
                   count(*) as room_nights
            from generate_series(%s::date, %s::date, '1 day') gs
            left join reservations r
              on r.property_id = %s
              and r.status in ('confirmed', 'checked_out')
              and r.check_in <= gs::date
              and r.check_out > gs::date
            group by gs::date
            order by gs::date
        """, (today, horizon, prop['id']))

        for d in daily:
            night = d['night']
            days_out = (night - today).days
            booked = d['room_nights'] or 0
            occ = (booked / units * 100) if units else 0

            # Python weekday: Mon=0..Sun=6 → Postgres dow: Sun=0..Sat=6
            pg_dow = (night.weekday() + 1) % 7
            baseline = rate_by_dow.get(pg_dow) or overall_rate

            if occ >= 70:
                bucket = "raise"
                if occ >= 95: pct, sug = 0.20, "↑ +15–25% (very full)"
                elif occ >= 85: pct, sug = 0.12, "↑ +10–15% (filling fast)"
                else: pct, sug = 0.07, "↑ +5–10%"
            elif days_out <= 14 and occ <= 30:
                bucket = "drop"
                if occ == 0 and days_out <= 7: pct, sug = -0.17, "↓ −15–20% (empty close-in)"
                elif occ <= 15: pct, sug = -0.12, "↓ −10–15%"
                else: pct, sug = -0.07, "↓ −5–10%"
            else:
                bucket = "steady"
                pct, sug = 0, "Hold"

            if baseline > 0 and pct != 0:
                target = baseline * (1 + pct)
                rate_text = f"€{baseline:.0f} → €{target:.0f}"
            elif baseline > 0:
                rate_text = f"€{baseline:.0f}"
            else:
                rate_text = "—"

            rows.append({
                "Date": night,
                "Day": night.strftime("%a"),
                "Property": prop['name'],
                "Booked": f"{booked}/{units}",
                "Occ %": f"{occ:.0f}%",
                "Days out": days_out,
                "Current → Target": rate_text,
                "Suggestion": sug,
                "_bucket": bucket,
            })

    df_all = pd.DataFrame(rows)
    if df_all.empty:
        st.info("No data available.")
        return

    st.subheader("🔥 RAISE RATES — high demand")
    raise_df = df_all[df_all["_bucket"] == "raise"].drop(columns=["_bucket"])
    if not raise_df.empty:
        st.dataframe(raise_df.sort_values("Date"), use_container_width=True, hide_index=True)
        st.caption(f"💡 {len(raise_df)} dates with ≥70% occupancy. Baseline rate = avg paid for that day-of-week over last 90 days.")
    else:
        st.success("✅ No 'raise' opportunities — nothing above 70% booked in this window.")

    st.markdown("---")

    st.subheader("⚠️ DROP RATES — close-in soft demand")
    drop_df = df_all[df_all["_bucket"] == "drop"].drop(columns=["_bucket"])
    if not drop_df.empty:
        st.dataframe(drop_df.sort_values("Date"), use_container_width=True, hide_index=True)
        st.caption(f"💡 {len(drop_df)} dates within 14 days are ≤30% booked. A 10–20% drop often fills these.")
    else:
        st.success("✅ Near-term occupancy is healthy — no urgent drops needed.")

    st.markdown("---")

    steady_count = int((df_all["_bucket"] == "steady").sum())
    st.subheader(f"✅ STEADY — {steady_count} dates trending normally")
    with st.expander("Show steady dates"):
        steady_df = df_all[df_all["_bucket"] == "steady"].drop(columns=["_bucket"])
        st.dataframe(steady_df.sort_values("Date"), use_container_width=True, hide_index=True)
# Page: Settings
# ============================================================================

def page_settings():
    st.title("⚙️ Settings")

    st.subheader("Properties in scope")
    props_df = df("""
        select name, location, currency, mgmt_fee_pct, cleaning_fee_per_turnover,
               linen_fee_per_turnover, total_units, active
        from properties
        order by name
    """)
    st.dataframe(props_df, use_container_width=True, hide_index=True)

    st.subheader("Last 10 sync runs")
    syncs = df("""
        select started_at, finished_at, status, records_upserted, error_message
        from sync_runs
        order by started_at desc
        limit 10
    """)
    st.dataframe(syncs, use_container_width=True, hide_index=True)

    st.subheader("Database info")
    total_res = q("select count(*) as n from reservations")[0]['n']
    st.write(f"Total reservations stored: **{total_res:,}**")

    st.subheader("OTA commission rates (used for fee estimates)")
    st.json(OTA_RATES)


# ============================================================================
# Route
# ============================================================================

if page == "📊 Portfolio Command Centre":
    page_portfolio()
elif page == "🎯 Pricing Opportunities":
    page_pricing()
elif page == "📈 Direct vs OTA":
    page_direct_vs_ota()
elif page == "📉 Performance Analytics":
    page_performance()
elif page == "⚙️ Settings":
    page_settings()
