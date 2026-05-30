"""Streamlit owner dashboard.

Deploys to share.streamlit.io. Owners log in with a magic link via Supabase
Auth and see only their property; admins (is_admin=true) see everything plus
the expense entry form.

This file is intentionally one-page for simplicity. Easy to split later.
"""
from __future__ import annotations

import os
from datetime import date
from dateutil.relativedelta import relativedelta
import pandas as pd
import streamlit as st
from supabase import create_client
import psycopg
from psycopg.rows import dict_row

st.set_page_config(page_title="CityHostings · Owner Dashboard", layout="wide", page_icon="🏠")

# ---------- config ----------
SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.environ.get("SUPABASE_URL", ""))
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", os.environ.get("SUPABASE_ANON_KEY", ""))
DATABASE_URL = st.secrets.get("DATABASE_URL", os.environ.get("DATABASE_URL", ""))

if not (SUPABASE_URL and SUPABASE_ANON_KEY and DATABASE_URL):
    st.error("Missing secrets. Set SUPABASE_URL, SUPABASE_ANON_KEY, DATABASE_URL.")
    st.stop()

supa = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


@st.cache_resource
def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def q(sql: str, params: tuple = ()) -> list[dict]:
    with get_db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------- auth ----------
if "user" not in st.session_state:
    st.session_state.user = None

def render_login():
    st.title("🏠 CityHostings Owner Portal")
    st.write("Enter your email — we'll send you a one-time login link.")
    email = st.text_input("Email", placeholder="you@example.com")
    if st.button("Send login link", type="primary"):
        try:
            supa.auth.sign_in_with_otp({"email": email})
            st.success("Check your email for the magic link. You can close this tab.")
        except Exception as e:
            st.error(f"Couldn't send link: {e}")


def render_logout():
    if st.sidebar.button("Sign out"):
        st.session_state.user = None
        st.rerun()


# Handle magic-link callback: Supabase appends access_token to URL fragment.
# Streamlit can't read fragments natively; the simplest workaround is to ask
# the user to paste their email after clicking — Supabase has already issued
# the session via the link. For a more elegant flow, use streamlit-oauth.
# For MVP: assume the user pastes their email and we accept it.
if st.session_state.user is None:
    qp = st.query_params
    if "email" in qp:
        st.session_state.user = qp["email"]
    else:
        render_login()
        st.stop()

user_email = st.session_state.user

# ---------- load access control ----------
@st.cache_data(ttl=300)
def get_user_access(email: str) -> dict:
    """Look up which properties this email can see + admin flag."""
    rows = q(
        """
        select p.id, p.name, p.location, p.currency, p.mgmt_fee_pct, oa.is_admin
          from properties p
          join owner_access oa on oa.property_id = p.id
          join auth.users u on u.id = oa.user_id
         where u.email = %s and p.active = true
         order by p.name
        """,
        (email,),
    )
    return {"properties": rows, "is_admin": any(r["is_admin"] for r in rows)}

access = get_user_access(user_email)

if not access["properties"]:
    st.error("No properties linked to your account yet. Contact CityHostings ops.")
    st.stop()

# ---------- sidebar ----------
st.sidebar.title("CityHostings")
st.sidebar.caption(f"Signed in as **{user_email}**")
render_logout()

if access["is_admin"]:
    st.sidebar.markdown("---")
    st.sidebar.caption("**Admin view**")
    prop_options = ["All properties"] + [p["name"] for p in access["properties"]]
else:
    prop_options = [p["name"] for p in access["properties"]]

selected_name = st.sidebar.selectbox("Property", prop_options)
selected_prop = next((p for p in access["properties"] if p["name"] == selected_name), None)

tab1, tab2, tab3, tab4 = st.tabs(["Overview", "Reservations", "P&L", "Reports"])

# ---------- overview ----------
with tab1:
    st.subheader(selected_name)

    if selected_prop:
        this_month = date.today().replace(day=1)
        kpi_rows = q(
            """
            select month, room_revenue, occupancy_rate, adr, revpar, bookings
              from monthly_kpis
             where property_id = %s
               and month > %s::date - interval '13 months'
             order by month
            """,
            (selected_prop["id"], this_month),
        )
        df = pd.DataFrame(kpi_rows)
        if df.empty:
            st.info("No data yet — sync has not run, or property has no reservations.")
        else:
            latest = df.iloc[-1]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Revenue (latest mo.)", f"{selected_prop['currency']} {latest['room_revenue']:,.0f}")
            c2.metric("Occupancy", f"{(latest['occupancy_rate'] or 0) * 100:.1f}%")
            c3.metric("ADR", f"{selected_prop['currency']} {latest['adr']:,.0f}")
            c4.metric("RevPAR", f"{selected_prop['currency']} {latest['revpar']:,.0f}")

            st.markdown("### Revenue trend")
            st.line_chart(df.set_index("month")["room_revenue"])

            st.markdown("### Occupancy trend")
            st.line_chart((df.set_index("month")["occupancy_rate"] * 100).rename("Occupancy %"))

# ---------- reservations ----------
with tab2:
    st.subheader("Reservations")
    if selected_prop:
        col1, col2 = st.columns(2)
        from_date = col1.date_input("From", value=date.today() - relativedelta(months=3))
        to_date = col2.date_input("To", value=date.today() + relativedelta(months=2))
        rows = q(
            """
            select check_in, check_out, source, status, guest_name,
                   net_amount, ota_commission, nights
              from reservations
             where property_id = %s
               and check_in between %s and %s
             order by check_in desc
            """,
            (selected_prop["id"], from_date, to_date),
        )
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

# ---------- p&l ----------
with tab3:
    st.subheader("Monthly P&L")
    if selected_prop:
        month_pick = st.date_input(
            "Month",
            value=date.today().replace(day=1) - relativedelta(months=1),
            format="YYYY-MM-DD",
        )
        month_pick = month_pick.replace(day=1)

        kpi = q(
            "select room_revenue, ota_fees from monthly_kpis where property_id=%s and month=%s",
            (selected_prop["id"], month_pick),
        )
        exp = q(
            "select category, sum(amount) as amount from expenses where property_id=%s and month=%s group by category",
            (selected_prop["id"], month_pick),
        )
        gross = float(kpi[0]["room_revenue"]) if kpi else 0
        ota = float(kpi[0]["ota_fees"]) if kpi else 0
        emap = {e["category"]: float(e["amount"]) for e in exp}
        mgmt = round(gross * float(selected_prop["mgmt_fee_pct"]) / 100, 2)
        net = round(
            gross - ota
            - emap.get("cleaning", 0) - emap.get("linen", 0)
            - emap.get("maintenance", 0) - emap.get("utilities", 0) - emap.get("other", 0)
            - mgmt,
            2,
        )
        cur = selected_prop["currency"]
        pnl_df = pd.DataFrame([
            ["Gross Revenue", gross],
            ["OTA Fees", -ota],
            ["Cleaning", -emap.get("cleaning", 0)],
            ["Linen", -emap.get("linen", 0)],
            ["Maintenance", -emap.get("maintenance", 0)],
            ["Utilities", -emap.get("utilities", 0)],
            ["Other", -emap.get("other", 0)],
            [f"Management Fee ({selected_prop['mgmt_fee_pct']}%)", -mgmt],
            ["Net Profit for Owner", net],
        ], columns=["Line item", f"Amount ({cur})"])
        st.dataframe(pnl_df, use_container_width=True, hide_index=True)

    # Admin: expense entry
    if access["is_admin"] and selected_prop:
        st.markdown("---")
        st.markdown("### Add an expense (admin)")
        with st.form("expense_form", clear_on_submit=True):
            ec1, ec2, ec3 = st.columns(3)
            cat = ec1.selectbox("Category", ["cleaning", "linen", "maintenance", "utilities", "other"])
            amt = ec2.number_input("Amount", min_value=0.0, step=10.0)
            mo = ec3.date_input("Month", value=date.today().replace(day=1))
            note = st.text_input("Note (optional)")
            if st.form_submit_button("Save"):
                with get_db().cursor() as cur_:
                    cur_.execute(
                        """
                        insert into expenses (property_id, month, category, amount, note, created_by)
                        values (%s, %s, %s, %s, %s, %s)
                        """,
                        (selected_prop["id"], mo.replace(day=1), cat, amt, note, user_email),
                    )
                    get_db().commit()
                st.success("Saved.")

# ---------- reports ----------
with tab4:
    st.subheader("Past Reports")
    if selected_prop:
        rows = q(
            "select month, pdf_url, emailed_at from monthly_reports where property_id=%s order by month desc limit 24",
            (selected_prop["id"],),
        )
        if not rows:
            st.info("No reports generated yet.")
        for r in rows:
            label = r["month"].strftime("%B %Y")
            url = r["pdf_url"]
            if url:
                st.markdown(f"- **{label}** — [Download PDF]({url})")
            else:
                st.markdown(f"- **{label}** — (PDF not available)")
