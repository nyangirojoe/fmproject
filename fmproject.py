# fm_streamlit.py
# Run:  streamlit run fm_streamlit.py
# Optional DB path override:  FM_DB=/path/to/facility.db streamlit run fm_streamlit.py

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

APP_TITLE = "Facility Management — Lightweight FM"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "facility.db")


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def T(col, label, width=150):
    return {"col": col, "label": label, "kind": "text", "width": width}


def N(col, label, width=90):
    return {"col": col, "label": label, "kind": "number", "width": width}


def D(col, label, width=110):
    return {"col": col, "label": label, "kind": "date", "width": width}


def C(col, label, options, width=130):
    return {"col": col, "label": label, "kind": "choice", "options": options, "width": width}


def H(col):
    return {"col": col, "label": col, "kind": "text", "width": 90, "hidden": True}


def HN(col):
    return {"col": col, "label": col, "kind": "number", "width": 90, "hidden": True}


# ---------------------------------------------------------------------------
# Auto-calculations
# ---------------------------------------------------------------------------
def auto_boq(data):
    if data.get("amount") is None:
        data["amount"] = round((data.get("qty") or 0) * (data.get("rate") or 0), 2)
    return data


def auto_sv(data):
    if data.get("total_cost") is None:
        data["total_cost"] = round((data.get("qty") or 0) * (data.get("unit_cost") or 0), 2)
    return data


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SQL_TYPE = {"text": "TEXT", "number": "REAL", "date": "TEXT", "choice": "TEXT"}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def create_schema(self, modules):
        for m in modules:
            cols = ", ".join(f'"{f["col"]}" {SQL_TYPE[f["kind"]]}' for f in m["fields"])
            self.conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{m["table"]}" '
                f"(id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})"
            )
            existing = {
                r["name"]
                for r in self.conn.execute(f'PRAGMA table_info("{m["table"]}")').fetchall()
            }
            for f in m["fields"]:
                if f["col"] not in existing:
                    self.conn.execute(
                        f'ALTER TABLE "{m["table"]}" ADD COLUMN '
                        f'"{f["col"]}" {SQL_TYPE[f["kind"]]}'
                    )
        self.conn.commit()

    def fetch(self, table, search=None):
        rows = self.conn.execute(f'SELECT * FROM "{table}" ORDER BY id DESC').fetchall()
        rows = [dict(r) for r in rows]
        if search:
            s = search.lower()
            rows = [
                r for r in rows
                if any(s in str(v).lower() for v in r.values() if v is not None)
            ]
        return rows

    def insert(self, table, data):
        cols = ", ".join(f'"{k}"' for k in data)
        marks = ", ".join("?" for _ in data)
        cur = self.conn.execute(
            f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', list(data.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def update(self, table, rid, data):
        sets = ", ".join(f'"{k}" = ?' for k in data)
        self.conn.execute(
            f'UPDATE "{table}" SET {sets} WHERE id = ?',
            list(data.values()) + [rid],
        )
        self.conn.commit()

    def delete(self, table, rid):
        self.conn.execute(f'DELETE FROM "{table}" WHERE id = ?', (rid,))
        self.conn.commit()

    def count(self, table):
        return self.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


# ---------------------------------------------------------------------------
# Stock ledger helpers + PBF / PRF interlink hooks
# ---------------------------------------------------------------------------
def _stock_on_hand(db, part_no):
    if not part_no:
        return 0.0
    rows = db.conn.execute(
        "SELECT voucher_type, qty FROM sv_stock_voucher WHERE part_no = ?",
        (part_no,),
    ).fetchall()
    bal = 0.0
    for r in rows:
        vt = (r["voucher_type"] or "").strip().lower()
        q = r["qty"] or 0.0
        if vt in ("grn", "return"):
            bal += q
        elif vt == "issue":
            bal -= q
        elif vt in ("transfer", "adjustment"):
            bal += q
    return bal


def _next_sv_no(db):
    row = db.conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM sv_stock_voucher"
    ).fetchone()
    return f"SV-{int(row['n']):05d}"


def _create_auto_sv(db, *, vtype, when, part_no, description, qty, uom,
                    store, issued_to, remarks, ref_table, ref_id):
    sv = {
        "voucher_no": _next_sv_no(db),
        "voucher_type": vtype,
        "voucher_date": when or datetime.now().strftime("%Y-%m-%d"),
        "part_no": part_no,
        "description": description,
        "qty": qty,
        "uom": uom,
        "unit_cost": None,
        "total_cost": None,
        "store": store,
        "issued_to": issued_to,
        "remarks": remarks,
        "ref_table": ref_table,
        "ref_id": ref_id,
    }
    return db.insert("sv_stock_voucher", sv)


def _delete_auto_sv(db, ref_table, ref_id):
    db.conn.execute(
        "DELETE FROM sv_stock_voucher WHERE ref_table = ? AND ref_id = ?",
        (ref_table, ref_id),
    )
    db.conn.commit()


def _refresh_pbf_status(db, borrow_no):
    if not borrow_no:
        return
    pbf = db.conn.execute(
        "SELECT * FROM pbf_part_borrowing WHERE borrow_no = ?", (borrow_no,)
    ).fetchone()
    if pbf is None:
        return
    borrowed = pbf["qty"] or 0.0
    returned = db.conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS s FROM prf_part_return WHERE borrow_no = ?",
        (borrow_no,),
    ).fetchone()["s"] or 0.0
    if returned <= 0:
        status = "Open"
    elif returned < borrowed:
        status = "Partially Returned"
    else:
        status = "Returned"
    db.conn.execute(
        "UPDATE pbf_part_borrowing SET status = ? WHERE id = ?",
        (status, pbf["id"]),
    )
    db.conn.commit()


def pbf_pre_save(db, data, record_id):
    if not data.get("part_no"):
        return False, "Part No. is required."
    if not data.get("qty") or data["qty"] <= 0:
        return False, "Quantity must be greater than zero."
    if record_id is None:
        available = _stock_on_hand(db, data["part_no"])
        if available < data["qty"]:
            return False, (
                f"Insufficient stock for part **{data['part_no']}**.\n\n"
                f"- On hand: `{available:g}`\n"
                f"- Requested: `{data['qty']:g}`\n\n"
                "Add stock first via a GRN voucher in the **SV** tab."
            )
    return True, ""


def pbf_post_save(db, record_id, data, is_new):
    if not is_new:
        return
    borrow_no = data.get("borrow_no") or f"PBF-{record_id}"
    _create_auto_sv(
        db,
        vtype="Issue",
        when=data.get("borrow_date"),
        part_no=data.get("part_no"),
        description=data.get("description"),
        qty=data.get("qty"),
        uom=data.get("uom"),
        store=data.get("source") or "Main Store",
        issued_to=data.get("issued_to"),
        remarks=f"Auto-issue for borrowing {borrow_no}",
        ref_table="pbf_part_borrowing",
        ref_id=record_id,
    )
    _refresh_pbf_status(db, borrow_no)


def pbf_post_delete(db, record_id, data):
    _delete_auto_sv(db, "pbf_part_borrowing", record_id)
    borrow_no = data.get("borrow_no")
    if borrow_no:
        prfs = db.conn.execute(
            "SELECT id FROM prf_part_return WHERE borrow_no = ?", (borrow_no,)
        ).fetchall()
        for r in prfs:
            _delete_auto_sv(db, "prf_part_return", r["id"])
        db.conn.execute(
            "DELETE FROM prf_part_return WHERE borrow_no = ?", (borrow_no,)
        )
        db.conn.commit()


def prf_pre_save(db, data, record_id):
    borrow_no = (data.get("borrow_no") or "").strip()
    if not borrow_no:
        return False, "Ref. Borrow No. is required to link a return to a borrowing."
    pbf = db.conn.execute(
        "SELECT * FROM pbf_part_borrowing WHERE borrow_no = ?", (borrow_no,)
    ).fetchone()
    if pbf is None:
        return False, f"No borrowing found with Borrow No. **{borrow_no}**."
    if data.get("qty") is None or data["qty"] <= 0:
        return False, "Return quantity must be greater than zero."
    sql = "SELECT COALESCE(SUM(qty), 0) AS s FROM prf_part_return WHERE borrow_no = ?"
    params = [borrow_no]
    if record_id is not None:
        sql += " AND id != ?"
        params.append(record_id)
    returned = db.conn.execute(sql, params).fetchone()["s"] or 0.0
    borrowed = pbf["qty"] or 0.0
    outstanding = borrowed - returned
    if data["qty"] > outstanding:
        return False, (
            f"Return quantity ({data['qty']:g}) exceeds outstanding "
            f"({outstanding:g}) for borrow **{borrow_no}**."
        )
    if pbf["part_no"] and not data.get("part_no"):
        data["part_no"] = pbf["part_no"]
    if pbf["description"] and not data.get("description"):
        data["description"] = pbf["description"]
    return True, ""


def prf_post_save(db, record_id, data, is_new):
    if is_new:
        _create_auto_sv(
            db,
            vtype="Return",
            when=data.get("return_date"),
            part_no=data.get("part_no"),
            description=data.get("description"),
            qty=data.get("qty"),
            uom=None,
            store="Main Store",
            issued_to=data.get("received_by"),
            remarks=f"Auto-return for borrowing {data.get('borrow_no')}",
            ref_table="prf_part_return",
            ref_id=record_id,
        )
    _refresh_pbf_status(db, data.get("borrow_no"))


def prf_post_delete(db, record_id, data):
    _delete_auto_sv(db, "prf_part_return", record_id)
    if data.get("borrow_no"):
        _refresh_pbf_status(db, data["borrow_no"])


def sv_pre_delete(db, record_id, data):
    if data.get("ref_table"):
        return False, (
            f"This stock voucher was auto-generated from "
            f"**{data['ref_table']}** (id {int(data.get('ref_id') or 0)}).\n\n"
            "Delete the source PBF / PRF record instead."
        )
    return True, ""


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------
MODULES = [
    {
        "name": "Asset Register",
        "table": "asset_register",
        "fields": [
            T("asset_code", "Asset Code", 110), T("asset_name", "Asset Name", 180),
            C("category", "Category", ["HVAC", "Electrical", "Plumbing", "Fire & Safety",
                                       "Elevator", "Civil / Structural", "IT / ELV",
                                       "Kitchen", "Other"], 130),
            T("location", "Location", 150), T("manufacturer", "Manufacturer", 130),
            T("model", "Model", 110), T("serial_no", "Serial No.", 120),
            D("install_date", "Install Date"),
            C("status", "Status", ["Active", "Under Maintenance", "Out of Service", "Retired"], 130),
            T("remarks", "Remarks", 200),
        ],
    },
    {
        "name": "Contracts",
        "table": "contracts",
        "fields": [
            T("contract_no", "Contract No.", 120), T("title", "Title", 200),
            T("contractor", "Contractor", 170),
            C("contract_type", "Type", ["PPM", "CM", "PPM + CM", "Consultancy", "Supply", "Other"], 120),
            D("start_date", "Start Date"), D("end_date", "End Date"),
            N("value", "Value", 110), T("currency", "Currency", 80),
            C("status", "Status", ["Draft", "Active", "Expiring", "Expired", "Terminated"], 120),
            T("remarks", "Remarks", 180),
        ],
    },
    {
        "name": "CM — Corrective",
        "table": "cm_work_orders",
        "fields": [
            T("wo_no", "WO No.", 110), T("asset_code", "Asset Code", 110),
            D("reported_date", "Reported"), T("description", "Fault / Description", 240),
            C("priority", "Priority", ["Low", "Medium", "High", "Critical"], 100),
            T("assigned_to", "Assigned To", 130),
            C("status", "Status", ["Open", "In Progress", "On Hold", "Completed", "Closed"], 120),
            D("completion_date", "Completed"), N("cost", "Cost", 90),
            T("remarks", "Remarks", 180),
        ],
    },
    {
        "name": "PPM — Preventive",
        "table": "ppm_schedule",
        "fields": [
            T("ppm_no", "PPM No.", 110), T("asset_code", "Asset Code", 110),
            T("task", "Task / Activity", 240),
            C("frequency", "Frequency", ["Daily", "Weekly", "Monthly", "Quarterly",
                                          "Semi-Annual", "Annual", "Bi-Annual"], 110),
            D("planned_date", "Planned"), D("last_done", "Last Done"),
            D("next_due", "Next Due"), T("assigned_to", "Assigned To", 130),
            C("status", "Status", ["Scheduled", "In Progress", "Completed", "Overdue", "Skipped"], 120),
            T("remarks", "Remarks", 160),
        ],
    },
    {
        "name": "PBF — Part Borrowing",
        "table": "pbf_part_borrowing",
        "fields": [
            T("borrow_no", "Borrow No.", 110), T("part_no", "Part No.", 110),
            T("description", "Description", 200), N("qty", "Qty", 70),
            T("uom", "UOM", 70), T("source", "Borrowed From", 150),
            T("issued_to", "Issued To", 130), D("borrow_date", "Borrow Date"),
            D("expected_return", "Expected Return"), T("authorised_by", "Authorised By", 130),
            C("status", "Status", ["Open", "Partially Returned", "Returned", "Overdue"], 140),
        ],
        "pre_save": pbf_pre_save, "post_save": pbf_post_save, "post_delete": pbf_post_delete,
    },
    {
        "name": "PRF — Part Return",
        "table": "prf_part_return",
        "fields": [
            T("return_no", "Return No.", 110), T("borrow_no", "Ref. Borrow No.", 120),
            T("part_no", "Part No.", 110), T("description", "Description", 200),
            N("qty", "Qty Returned", 100), D("return_date", "Return Date"),
            T("received_by", "Received By", 130),
            C("condition", "Condition", ["Good", "Used", "Damaged", "Scrap"], 110),
            T("remarks", "Remarks", 180),
        ],
        "pre_save": prf_pre_save, "post_save": prf_post_save, "post_delete": prf_post_delete,
    },
    {
        "name": "SV — Stock Voucher",
        "table": "sv_stock_voucher",
        "fields": [
            T("voucher_no", "Voucher No.", 110),
            C("voucher_type", "Voucher Type", ["GRN", "Issue", "Transfer", "Return", "Adjustment"], 120),
            D("voucher_date", "Date"), T("part_no", "Part No.", 110),
            T("description", "Description", 200), N("qty", "Qty", 70),
            T("uom", "UOM", 70), N("unit_cost", "Unit Cost", 90),
            N("total_cost", "Total Cost", 100), T("store", "Store", 120),
            T("issued_to", "Issued To / Supplier", 160), T("remarks", "Remarks", 150),
            H("ref_table"), HN("ref_id"),
        ],
        "auto": auto_sv, "pre_delete": sv_pre_delete,
    },
    {
        "name": "SOR — Rates",
        "table": "sor_schedule_of_rates",
        "fields": [
            T("sor_code", "SOR Code", 110), T("description", "Description", 260),
            T("trade", "Trade / Discipline", 150), T("unit", "Unit", 80),
            N("rate", "Rate", 100), T("currency", "Currency", 80),
            D("effective_date", "Effective From"), T("remarks", "Remarks", 160),
        ],
    },
    {
        "name": "BOQ — Quantities",
        "table": "boq_bill_of_quantities",
        "fields": [
            T("boq_no", "BOQ No.", 110), T("project", "Project", 170),
            T("sor_code", "SOR Code", 110), T("description", "Description", 240),
            T("unit", "Unit", 70), N("qty", "Qty", 80), N("rate", "Rate", 90),
            N("amount", "Amount", 110), T("remarks", "Remarks", 160),
        ],
        "auto": auto_boq,
    },
]


# ---------------------------------------------------------------------------
# Session DB singleton
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db(path: str) -> Database:
    db = Database(path)
    db.create_schema(MODULES)
    return db


# ---------------------------------------------------------------------------
# Form field rendering
# ---------------------------------------------------------------------------
def _num_or_none(raw: str):
    raw = raw.strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"'{raw}' is not a valid number.")


def render_form_inputs(module, defaults: dict | None = None) -> dict:
    defaults = defaults or {}
    visible = [f for f in module["fields"] if not f.get("hidden")]
    data: dict = {}

    cols = st.columns(3)
    for i, f in enumerate(visible):
        with cols[i % 3]:
            key = f"f_{module['table']}_{f['col']}"
            default = defaults.get(f["col"], "") or ""
            if f["kind"] == "number":
                default_str = f"{default:g}" if isinstance(default, float) else str(default or "")
                raw = st.text_input(f["label"], value=default_str, key=key)
                try:
                    data[f["col"]] = _num_or_none(raw)
                except ValueError:
                    data[f["col"]] = None
            elif f["kind"] == "choice":
                opts = [""] + list(f["options"])
                idx = opts.index(default) if default in opts else 0
                data[f["col"]] = st.selectbox(
                    f["label"], opts, index=idx, key=key
                ) or None
            elif f["kind"] == "date":
                raw = st.text_input(
                    f["label"], value=str(default or ""),
                    placeholder="YYYY-MM-DD", key=key,
                )
                data[f["col"]] = raw.strip() or None
            else:
                data[f["col"]] = st.text_input(
                    f["label"], value=str(default or ""), key=key
                ).strip() or None

    for f in module["fields"]:
        if f.get("hidden") and f["col"] not in data:
            data[f["col"]] = defaults.get(f["col"])
    return data


def validate_dates(module, data) -> str | None:
    for f in module["fields"]:
        if f["kind"] == "date" and data.get(f["col"]):
            try:
                datetime.strptime(data[f["col"]], "%Y-%m-%d")
            except (ValueError, TypeError):
                return f"**{f['label']}** must be a date in `YYYY-MM-DD` format."
    return None


def validate_numbers(module, data) -> str | None:
    for f in module["fields"]:
        if f["kind"] != "number":
            continue
        key = f"f_{module['table']}_{f['col']}"
        raw = str(st.session_state.get(key, "")).strip()
        if raw == "":
            continue
        try:
            float(raw)
        except ValueError:
            return f"**{f['label']}** must be a number (got `{raw}`)."
    return None


# ---------------------------------------------------------------------------
# Module page
# ---------------------------------------------------------------------------
def _row_defaults(module, row: dict | None) -> dict:
    if not row:
        return {}
    return {f["col"]: row.get(f["col"]) for f in module["fields"]}


def _render_records_table(module, rows):
    if not rows:
        st.info("No records yet.")
        return None
    visible = [f for f in module["fields"] if not f.get("hidden")]
    df = pd.DataFrame(
        [{f["label"]: r.get(f["col"]) for f in visible} | {"id": r["id"]} for r in rows]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)

    ids = [r["id"] for r in rows]

    def _label(r):
        bits = [f"#{r['id']}"]
        for f in visible[:3]:
            v = r.get(f["col"])
            if v:
                bits.append(str(v))
        return "  ·  ".join(bits)

    labels = {r["id"]: _label(r) for r in rows}
    sel = st.selectbox(
        "Select a row to edit / delete",
        options=ids,
        format_func=lambda i: labels.get(i, f"#{i}"),
        index=0,
        key=f"select_{module['table']}",
    )
    return next((r for r in rows if r["id"] == sel), None)


def render_module(module, db: Database):
    st.subheader(module["name"])
    st.caption(f"Table: `{module['table']}` — {db.count(module['table'])} record(s)")

    search = st.text_input(
        "🔍 Search (any column)",
        value=st.session_state.get(f"search_{module['table']}", ""),
        key=f"search_{module['table']}",
    )
    rows = db.fetch(module["table"], search.strip() or None)

    selected = _render_records_table(module, rows)

    st.divider()

    with st.expander("➕ Add new record", expanded=selected is None):
        with st.form(key=f"new_{module['table']}", clear_on_submit=True):
            new_data = render_form_inputs(module, defaults={})
            submitted = st.form_submit_button("Save (Add)")

        if submitted:
            err = validate_dates(module, new_data) or validate_numbers(module, new_data)
            if err:
                st.error(err)
            else:
                hook = module.get("auto")
                if hook:
                    new_data = hook(new_data)
                pre = module.get("pre_save")
                if pre:
                    ok, msg = pre(db, new_data, None)
                    if not ok:
                        st.error(msg)
                        return
                rid = db.insert(module["table"], new_data)
                post = module.get("post_save")
                if post:
                    post(db, rid, new_data, True)
                st.success(f"Record #{rid} added.")
                st.rerun()

    if selected is not None:
        st.markdown(f"### Editing record `#{selected['id']}`")
        defaults = _row_defaults(module, selected)

        with st.form(key=f"edit_{module['table']}_{selected['id']}"):
            edited = render_form_inputs(module, defaults=defaults)
            col1, col2, col3 = st.columns([1, 1, 4])
            with col1:
                save_btn = st.form_submit_button("💾 Update")
            with col2:
                del_btn = st.form_submit_button("🗑 Delete")

        if save_btn:
            err = validate_dates(module, edited) or validate_numbers(module, edited)
            if err:
                st.error(err)
            else:
                hook = module.get("auto")
                if hook:
                    edited = hook(edited)
                pre = module.get("pre_save")
                if pre:
                    ok, msg = pre(db, edited, selected["id"])
                    if not ok:
                        st.error(msg)
                        return
                db.update(module["table"], selected["id"], edited)
                post = module.get("post_save")
                if post:
                    post(db, selected["id"], edited, False)
                st.success(f"Record #{selected['id']} updated.")
                st.rerun()

        if del_btn:
            pre = module.get("pre_delete")
            if pre:
                ok, msg = pre(db, selected["id"], selected)
                if not ok:
                    st.error(msg)
                    return
            if st.session_state.get(f"confirm_del_{module['table']}_{selected['id']}"):
                db.delete(module["table"], selected["id"])
                post = module.get("post_delete")
                if post:
                    post(db, selected["id"], selected)
                st.session_state.pop(f"confirm_del_{module['table']}_{selected['id']}", None)
                st.success(f"Record #{selected['id']} deleted.")
                st.rerun()
            else:
                st.session_state[f"confirm_del_{module['table']}_{selected['id']}"] = True
                st.warning(
                    f"⚠️ Click **Delete** again to confirm removal of record "
                    f"`#{selected['id']}`."
                )


# ---------------------------------------------------------------------------
# Stock Balance page
# ---------------------------------------------------------------------------
def render_stock_balance(db: Database):
    st.subheader("Stock Balance")
    st.caption("Aggregated live from all **Stock Voucher** movements.")

    rows = db.conn.execute(
        "SELECT part_no, description, uom, voucher_type, qty "
        "FROM sv_stock_voucher "
        "WHERE part_no IS NOT NULL AND TRIM(part_no) != ''"
    ).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        p = (r["part_no"] or "").strip()
        if not p:
            continue
        if p not in agg:
            agg[p] = {
                "part_no": p, "description": r["description"] or "",
                "uom": r["uom"] or "", "in_qty": 0.0, "out_qty": 0.0,
            }
        else:
            if not agg[p]["description"] and r["description"]:
                agg[p]["description"] = r["description"]
            if not agg[p]["uom"] and r["uom"]:
                agg[p]["uom"] = r["uom"]

        vt = (r["voucher_type"] or "").strip().lower()
        q = r["qty"] or 0.0
        if vt in ("grn", "return"):
            agg[p]["in_qty"] += q
        elif vt == "issue":
            agg[p]["out_qty"] += q
        elif vt in ("transfer", "adjustment"):
            if q >= 0:
                agg[p]["in_qty"] += q
            else:
                agg[p]["out_qty"] += -q

    if not agg:
        st.info("No stock movements yet. Add a **GRN** voucher in the SV module to seed stock.")
        return

    records = []
    for p in sorted(agg):
        a = agg[p]
        records.append({
            "Part No.": a["part_no"],
            "Description": a["description"],
            "UOM": a["uom"],
            "Total In": a["in_qty"],
            "Total Out": a["out_qty"],
            "On Hand": a["in_qty"] - a["out_qty"],
        })
    df = pd.DataFrame(records)

    def _highlight(row):
        if row["On Hand"] <= 0:
            return ["background-color: #ffe0e0"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df.style.apply(_highlight, axis=1).format(
            {"Total In": "{:g}", "Total Out": "{:g}", "On Hand": "{:g}"}
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"{len(agg)} part(s)")


# ---------------------------------------------------------------------------
# App shell
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    db_path = os.environ.get("FM_DB", DEFAULT_DB)
    db = get_db(db_path)

    # ----- Header -----
    hdr_col1, hdr_col2 = st.columns([1, 6])
    with hdr_col1:
        st.markdown(
            """
            <svg width="52" height="52" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
              <circle cx="20" cy="20" r="18" fill="none" stroke="#3a3a3a" stroke-width="2.5"/>
              <line x1="17" y1="7"  x2="17" y2="33" stroke="#3a3a3a" stroke-width="6" stroke-linecap="round"/>
              <line x1="9"  y1="12" x2="22" y2="12" stroke="#3a3a3a" stroke-width="6" stroke-linecap="round"/>
              <line x1="9"  y1="20" x2="20" y2="20" stroke="#3a3a3a" stroke-width="6" stroke-linecap="round"/>
              <line x1="20" y1="25" x2="31" y2="14" stroke="#3a3a3a" stroke-width="6" stroke-linecap="round"/>
              <line x1="22" y1="16" x2="34" y2="22" stroke="#3a3a3a" stroke-width="6" stroke-linecap="round"/>
              <line x1="6"  y1="27" x2="25" y2="25" stroke="#3a3a3a" stroke-width="6" stroke-linecap="round"/>
            </svg>
            """,
            unsafe_allow_html=True,
        )
    with hdr_col2:
        st.markdown("## Facility Management")
        st.caption(f"DB: `{db_path}`")

    # ----- Sidebar module selector -----
    tab_labels = [m["name"] for m in MODULES]
    sv_idx = next(i for i, m in enumerate(MODULES) if m["table"] == "sv_stock_voucher")
    tab_labels.insert(sv_idx + 1, "Stock Balance")

    with st.sidebar:
        st.header("Modules")
        selected_tab = st.radio(
            "Select module",
            tab_labels,
            label_visibility="collapsed",
        )

    # ----- Render the selected module only -----
    if selected_tab == "Stock Balance":
        render_stock_balance(db)
    else:
        module = next(m for m in MODULES if m["name"] == selected_tab)
        render_module(module, db)


if __name__ == "__main__":
    main()
