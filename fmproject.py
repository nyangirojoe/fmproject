# fmproject.py
# Run:  streamlit run fmproject.py
# Deploy: Streamlit Community Cloud with Turso secrets configured.

from __future__ import annotations

import os
from datetime import datetime

import sqlite3
import pandas as pd
import streamlit as st
import turso_serverless

APP_TITLE = "LijiArts Ltd — Facility Management"


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
    
def AS(col, label, width=220):
    """Asset-name selector: dropdown of existing names + 'enter new' option."""
    return {"col": col, "label": label, "kind": "asset_select", "width": width}

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
# Database (Turso / libsql via SQLAlchemy)
# ---------------------------------------------------------------------------
SQL_TYPE = {
    "text": "TEXT",
    "number": "REAL",
    "date": "TEXT",
    "choice": "TEXT",
    "existing_select": "TEXT",
}

from turso_serverless import connect


def _turso_credentials():
    try:
        url = st.secrets["turso"]["url"]
        token = st.secrets["turso"]["token"]
    except (KeyError, FileNotFoundError):
        st.error(
            "**Turso credentials missing.** Add this to your Streamlit app's "
            "Secrets (Settings → Secrets):\n\n"
            "```toml\n[turso]\nurl = \"libsql://...\"\ntoken = \"...\"\n```"
        )
        st.stop()
    return url, token


class Database:
    """Turso-backed database using turso_serverless.

    Writes must be explicitly committed — remote Turso streams do not
    auto-commit, and uncommitted writes are rolled back when the HTTP
    stream expires. We also auto-reconnect on 'stream not found' errors.
    """

    def __init__(self):
        self._url, self._token = _turso_credentials()
        if self._url.startswith("libsql://"):
            self._url = "https://" + self._url[len("libsql://"):]
        self._connect()

    def _connect(self):
        self.conn = connect(self._url, auth_token=self._token)

    def _is_stream_error(self, exc):
        return "stream not found" in str(exc).lower()

    def _execute(self, sql, params=None):
        try:
            return self.conn.execute(sql, params or ())
        except Exception as exc:
            if not self._is_stream_error(exc):
                raise
            self._connect()
            return self.conn.execute(sql, params or ())

    def _commit(self):
        try:
            self.conn.commit()
        except Exception:
            # If commit fails because the stream died, reconnect and retry
            self._connect()
            self.conn.commit()

    def create_schema(self, modules):
        for m in modules:
            cols = ", ".join(
                f'"{f["col"]}" {SQL_TYPE.get(f["kind"], "TEXT")}' for f in m["fields"]
            )
            self._execute(
                f'CREATE TABLE IF NOT EXISTS "{m["table"]}" '
                f"(id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})"
            )
            # Migration: add any columns missing from an older schema
            info_cur = self._execute(f'PRAGMA table_info("{m["table"]}")')
            info = _TursoResult(info_cur).fetchall()
            existing = {row["name"] for row in info if "name" in row}
            for f in m["fields"]:
                if f["col"] not in existing:
                    self._execute(
                        f'ALTER TABLE "{m["table"]}" ADD COLUMN '
                        f'"{f["col"]}" {SQL_TYPE.get(f["kind"], "TEXT")}'
                    )
        self._commit()

    def _rows_to_dicts(self, cursor):
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description] if cursor.description else []
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(r)
            else:
                out.append(dict(zip(cols, r)))
        return out

    def fetch(self, table, search=None):
        cur = self._execute(f'SELECT * FROM "{table}" ORDER BY id DESC')
        records = self._rows_to_dicts(cur)
        if search:
            s = search.lower()
            records = [
                r for r in records
                if any(s in str(v).lower() for v in r.values() if v is not None)
            ]
        return records

    def insert(self, table, data):
        cols = ", ".join(f'"{k}"' for k in data)
        marks = ", ".join("?" for _ in data)
        cur = self._execute(
            f'INSERT INTO "{table}" ({cols}) VALUES ({marks})',
            list(data.values()),
        )
        rid = getattr(cur, "lastrowid", None)
        self._commit()
        if rid is not None and rid != 0:
            return rid
        cur = self._execute(f'SELECT MAX(id) AS rid FROM "{table}"')
        row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get("rid")
        return row[0]

    def update(self, table, rid, data):
        sets = ", ".join(f'"{k}" = ?' for k in data)
        self._execute(
            f'UPDATE "{table}" SET {sets} WHERE id = ?',
            list(data.values()) + [rid],
        )
        self._commit()

    def delete(self, table, rid):
        self._execute(f'DELETE FROM "{table}" WHERE id = ?', (rid,))
        self._commit()

    def delete_all(self, table):
        self._execute(f'DELETE FROM "{table}"')
        self._commit()

    def count(self, table):
        cur = self._execute(f'SELECT COUNT(*) AS c FROM "{table}"')
        row = cur.fetchone()
        if row is None:
            return 0
        if isinstance(row, dict):
            return row.get("c", 0)
        return row[0]

    def execute(self, sql, params=None):
        cur = self._execute(sql, params)
        return _TursoResult(cur)


class _TursoResult:
    def __init__(self, cursor):
        self._cursor = cursor
        self._rows = cursor.fetchall()
        self._cols = (
            [d[0] for d in cursor.description] if cursor.description else []
        )

    def _as_dict(self, row):
        if isinstance(row, dict):
            return row
        return dict(zip(self._cols, row))

    def fetchall(self):
        return [self._as_dict(r) for r in self._rows]

    def fetchone(self):
        if not self._rows:
            return None
        return self._as_dict(self._rows[0])

    def scalar(self):
        if not self._rows:
            return None
        r = self._rows[0]
        if isinstance(r, dict):
            return list(r.values())[0]
        return r[0]
# ---------------------------------------------------------------------------
# Stock ledger helpers + PBF / PRF interlink hooks
# ---------------------------------------------------------------------------

def _stock_on_hand(db, part_no):
    if not part_no:
        return 0.0
    rows = db.execute(
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
    row = db.execute(
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
    db.execute(
        "DELETE FROM sv_stock_voucher WHERE ref_table = ? AND ref_id = ?",
        (ref_table, ref_id),
    )


def _refresh_pbf_status(db, borrow_no):
    if not borrow_no:
        return
    pbf = db.execute(
        "SELECT * FROM pbf_part_borrowing WHERE borrow_no = ?",
        (borrow_no,),
    ).fetchone()
    if pbf is None:
        return
    borrowed = pbf["qty"] or 0.0
    row = db.execute(
        "SELECT COALESCE(SUM(qty), 0) AS s FROM prf_part_return WHERE borrow_no = ?",
        (borrow_no,),
    ).fetchone()
    returned = (row["s"] if row else 0) or 0.0
    if returned <= 0:
        status = "Open"
    elif returned < borrowed:
        status = "Partially Returned"
    else:
        status = "Returned"
    db.execute(
        "UPDATE pbf_part_borrowing SET status = ? WHERE id = ?",
        (status, pbf["id"]),
    )


def pbf_pre_save(db, data, record_id):
    if not data.get("part_no"):
        return False, "Part No. is required."
    qty = data.get("qty")
    if qty is None or qty <= 0:
        return False, "Quantity must be greater than zero."
    if record_id is None:
        available = _stock_on_hand(db, data["part_no"])
        if available < qty:
            return False, (
                f"Insufficient stock for part **{data['part_no']}**.\n\n"
                f"- On hand: `{available:g}`\n"
                f"- Requested: `{qty:g}`\n\n"
                "Add stock first via a GRN voucher in the **SV** module."
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
        prfs = db.execute(
            "SELECT id FROM prf_part_return WHERE borrow_no = ?",
            (borrow_no,),
        ).fetchall()
        for r in prfs:
            _delete_auto_sv(db, "prf_part_return", r["id"])
        db.execute(
            "DELETE FROM prf_part_return WHERE borrow_no = ?",
            (borrow_no,),
        )


def prf_pre_save(db, data, record_id):
    borrow_no = (data.get("borrow_no") or "").strip()
    if not borrow_no:
        return False, "Ref. Borrow No. is required to link a return to a borrowing."
    pbf = db.execute(
        "SELECT * FROM pbf_part_borrowing WHERE borrow_no = ?",
        (borrow_no,),
    ).fetchone()
    if pbf is None:
        return False, f"No borrowing found with Borrow No. **{borrow_no}**."
    qty = data.get("qty")
    if qty is None or qty <= 0:
        return False, "Return quantity must be greater than zero."
    if record_id is not None:
        row = db.execute(
            "SELECT COALESCE(SUM(qty), 0) AS s FROM prf_part_return "
            "WHERE borrow_no = ? AND id != ?",
            (borrow_no, record_id),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT COALESCE(SUM(qty), 0) AS s FROM prf_part_return "
            "WHERE borrow_no = ?",
            (borrow_no,),
        ).fetchone()
    returned = (row["s"] if row else 0) or 0.0
    borrowed = pbf["qty"] or 0.0
    outstanding = borrowed - returned
    if qty > outstanding:
        return False, (
            f"Return quantity ({qty:g}) exceeds outstanding "
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
            AS("asset_name", "Asset Name", 220),
            C("category", "Category", ["Room Management", "Fire Alarm"], 150),
            N("qty", "Qty", 70),
            C("status", "Status", ["Active", "Under Maintenance", "Out of Service", "Retired"], 130),
            T("contract_no", "Contract No.", 120),
            T("venue", "Venue", 130),
            T("remarks", "Remarks", 200),
        ],
    },
    {
        "name": "Contracts",
        "table": "contracts",
        "fields": [
            T("contract_no", "Contract No.", 120),
            C("contract_type", "Type", ["PPM", "CM", "PPM + CM", "Consultancy", "Supply", "Project and Service", "Other"], 120),
            D("start_date", "Start Date"), D("end_date", "End Date"),
            N("value", "Value", 110), T("currency", "Currency", 80),
            C("status", "Status", ["Draft", "Active", "Live", "Expiring", "Expired", "Terminated"], 120),
            T("remarks", "Remarks", 180),
        ],
    },
    
    {
        "name": "PPM — Preventive",
        "table": "ppm_schedule",
        "fields": [
            T("ppm_no", "PPM No.", 110),
            T("asset_name", "Asset Name", 180),
            T("contract_no", "Contract No.", 120),
            T("venue", "Venue", 130),
            C("frequency", "Frequency", ["Q", "Q,Y", "Q,HY,Y", "HY", "Y", "CM Only"], 130),
            D("planned_date", "Planned"), D("last_done", "Last Done"),
            D("next_due", "Next Due"), T("current_ppm", "Current PPM", 140),
            C("status", "Status", ["Scheduled", "In Progress", "Completed", "Overdue", "Skipped"], 120),
            N("qty", "Qty", 70),
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
def get_db() -> Database:
    db = Database()
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


def render_form_inputs(
    module, defaults: dict | None = None, prefix: str = "", db=None
) -> dict:
    defaults = defaults or {}
    visible = [f for f in module["fields"] if not f.get("hidden")]
    data: dict = {}

    cols = st.columns(3)
    for i, f in enumerate(visible):
        with cols[i % 3]:
            key = f"{prefix}f_{module['table']}_{f['col']}"
            default = defaults.get(f["col"], "") or ""

            if f["kind"] == "number":
                default_str = (
                    f"{default:g}" if isinstance(default, float) else str(default or "")
                )
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

            elif f["kind"] == "asset_select":
                existing: list[str] = []
                if db is not None:
                    try:
                        all_assets = db.fetch("asset_register")
                        existing = sorted({
                            (a.get("asset_name") or "").strip()
                            for a in all_assets
                            if (a.get("asset_name") or "").strip()
                        })
                    except Exception:
                        existing = []
                current = str(default or "").strip()
                if current and current not in existing:
                    existing = [current] + existing

                NEW_OPT = "➕ Enter new name…"
                options = [""] + existing + [NEW_OPT]
                if current and current in options:
                    idx = options.index(current)
                else:
                    idx = 0

                sel = st.selectbox(
                    f["label"], options, index=idx, key=key
                )

                if sel == NEW_OPT:
                    new_name = st.text_input(
                        f"{f['label']} — new name",
                        value="",
                        key=key + "_new",
                    ).strip()
                    data[f["col"]] = new_name or None
                else:
                    data[f["col"]] = sel or None

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
        try:
            raw = str(st.session_state.get(key, "")).strip()
        except Exception:
            continue
        if raw == "":
            continue
        try:
            float(raw)
        except ValueError:
            return f"**{f['label']}** must be a number (got `{raw}`)."
    return None


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

    # ---------- add new ----------
    with st.expander("➕ Add new record", expanded=selected is None):
        with st.form(key=f"new_{module['table']}", clear_on_submit=True):
            new_data = render_form_inputs(module, defaults={}, prefix="new_", db=db)
            submitted = st.form_submit_button("Save (Add)")

        if submitted:
            token = f"saved_{module['table']}"
            if st.session_state.get(token):
                st.info("A save is already in progress — please wait.")
            else:
                err = validate_dates(module, new_data) or validate_numbers(module, new_data)
                if err:
                    st.error(err)
                else:
                    st.session_state[token] = True
                    try:
                        hook = module.get("auto")
                        if hook:
                            new_data = hook(new_data)
                        pre = module.get("pre_save")
                        if pre:
                            ok, msg = pre(db, new_data, None)
                            if not ok:
                                st.error(msg)
                                st.session_state.pop(token, None)
                                return
                        rid = db.insert(module["table"], new_data)
                        post = module.get("post_save")
                        if post:
                            post(db, rid, new_data, True)
                        st.success(f"Record #{rid} added.")
                    except Exception as exc:
                        st.error(f"Save failed: {exc}")
                        st.session_state.pop(token, None)
                        return
                    finally:
                        st.session_state.pop(token, None)
                    st.rerun()

    # ---------- edit / delete single ----------
    if selected is not None:
        st.markdown(f"### Editing record `#{selected['id']}`")
        defaults = _row_defaults(module, selected)

        with st.form(key=f"edit_{module['table']}_{selected['id']}"):
            edited = render_form_inputs(module, defaults=defaults, prefix=f"edit_{selected['id']}_", db=db)
            col1, col2, _ = st.columns([1, 1, 4])
            with col1:
                save_btn = st.form_submit_button("💾 Update")
            with col2:
                del_btn = st.form_submit_button("🗑 Delete")

        if save_btn:
            err = validate_dates(module, edited) or validate_numbers(module, edited)
            if err:
                st.error(err)
            else:
                try:
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
                except Exception as exc:
                    st.error(f"Update failed: {exc}")

        if del_btn:
            pre = module.get("pre_delete")
            if pre:
                ok, msg = pre(db, selected["id"], selected)
                if not ok:
                    st.error(msg)
                    return
            confirm_key = f"confirm_del_{module['table']}_{selected['id']}"
            if st.session_state.get(confirm_key):
                try:
                    db.delete(module["table"], selected["id"])
                    post = module.get("post_delete")
                    if post:
                        post(db, selected["id"], selected)
                    st.session_state.pop(confirm_key, None)
                    st.success(f"Record #{selected['id']} deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
            else:
                st.session_state[confirm_key] = True
                st.warning(
                    f"⚠️ Click **Delete** again to confirm removal of record "
                    f"`#{selected['id']}`."
                )

    # ---------- bulk delete ----------
    st.divider()
    with st.expander("🧨 Danger zone — bulk delete"):
        total = db.count(module["table"])
        st.write(
            f"This deletes **all {total} record(s)** in `{module['table']}`. "
            "This cannot be undone."
        )
        confirm_bulk = st.checkbox(
            f"I understand this will wipe all {total} records in this module",
            key=f"bulk_ack_{module['table']}",
        )
        if st.button(
            f"Delete all records in {module['name']}",
            disabled=not confirm_bulk or total == 0,
            key=f"bulk_del_{module['table']}",
        ):
            try:
                db.delete_all(module["table"])
                st.success(f"All records deleted from {module['name']}.")
                st.rerun()
            except Exception as exc:
                st.error(f"Bulk delete failed: {exc}")
                # ---------------------------------------------------------------------------
# Stock Balance page
# ---------------------------------------------------------------------------
def render_stock_balance(db: Database):
    st.subheader("Stock Balance")
    st.caption("Aggregated live from all **Stock Voucher** movements.")

    rows = db.execute(
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
# PPM Generator page
# ---------------------------------------------------------------------------
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _month_abbr(date_str) -> str:
    """Return 'Oct' for '2025-10-01'; blank on empty or malformed input."""
    if not date_str or len(str(date_str)) < 7:
        return ""
    try:
        month_num = int(str(date_str)[5:7])
        if 1 <= month_num <= 12:
            return _MONTH_ABBR[month_num - 1]
    except (ValueError, IndexError):
        pass
    return ""


def _update_ppm_schedule_fields(db, asset_name, contract_no, venue):
    """Recompute last_done, next_due and current_ppm for all PPMs of one asset.

    Each field stores only the month abbreviation (e.g. 'Oct') — the year and
    day are visible in the Planned column if needed for context.
    """
    rows = db.execute(
        "SELECT * FROM ppm_schedule "
        "WHERE asset_name = ? AND contract_no = ? AND venue = ? "
        "ORDER BY planned_date",
        (asset_name or "", contract_no or "", venue or ""),
    ).fetchall()

    if not rows:
        return

    for i, p in enumerate(rows):
        last_done = _month_abbr(rows[i - 1].get("planned_date") or "") if i > 0 else ""
        next_due = _month_abbr(rows[i + 1].get("planned_date") or "") if i < len(rows) - 1 else ""
        current = _month_abbr(p.get("planned_date") or "")

        if (
            (p.get("last_done") or "") != last_done
            or (p.get("next_due") or "") != next_due
            or (p.get("current_ppm") or "") != current
        ):
            db.update("ppm_schedule", p["id"], {
                "last_done": last_done,
                "next_due": next_due,
                "current_ppm": current,
            })


def render_ppm_generator(db: Database):
    st.subheader("PPM Generator")
    st.caption("Generate and view PPMs by contract, venue, asset and month.")

    if "ppm_reset_counter" not in st.session_state:
        st.session_state.ppm_reset_counter = 0
    counter = st.session_state.ppm_reset_counter

    # ---------- contracts ----------
    contracts = db.fetch("contracts")
    contract_options = {}
    for c in contracts:
        cn = (c.get("contract_no") or "").strip()
        title = (c.get("title") or "").strip()
        label = f"{cn} — {title}" if cn and title else (cn or title or f"Contract #{c['id']}")
        contract_options[label] = c

    all_assets = db.fetch("asset_register")

    # ---------- row 1: contract / venue / reset ----------
    col_a, col_b, col_c = st.columns([2, 2, 1])

    with col_a:
        contract_sel = st.selectbox(
            "Select contract",
            options=[""] + list(contract_options.keys()),
            key=f"ppm_contract_sel_{counter}",
        )

    contract_no = ""
    if contract_sel:
        contract_no = contract_options[contract_sel].get("contract_no") or ""

    if contract_no:
        contract_assets = [
            a for a in all_assets if (a.get("contract_no") or "") == contract_no
        ]
    else:
        contract_assets = all_assets

    venues = sorted({
        (a.get("venue") or "").strip()
        for a in contract_assets
        if (a.get("venue") or "").strip()
    })

    with col_b:
        venue_sel = st.selectbox(
            "Select venue",
            options=[""] + venues,
            key=f"ppm_venue_sel_{counter}",
        )

    with col_c:
        st.write("")
        if st.button("🔄 Reset venue", key=f"ppm_reset_venue_btn_{counter}"):
            st.session_state.ppm_reset_counter += 1
            st.rerun()

    # ---------- venue filter ----------
    if venue_sel:
        stage_assets = [
            a for a in contract_assets
            if (a.get("venue") or "").strip() == venue_sel
        ]
    else:
        stage_assets = contract_assets

    # ---------- row 2: asset name / frequency ----------
    asset_names = sorted({
        (a.get("asset_name") or "").strip()
        for a in stage_assets
        if (a.get("asset_name") or "").strip()
    })

    col_d, col_f = st.columns([3, 2])

    with col_d:
        asset_sel = st.selectbox(
            "Select asset name",
            options=[""] + asset_names,
            key=f"ppm_asset_sel_{counter}_{venue_sel}",
        )

    if asset_sel:
        filtered_assets = [
            a for a in stage_assets
            if (a.get("asset_name") or "").strip() == asset_sel
        ]
    else:
        filtered_assets = stage_assets

    with col_f:
        freq = st.selectbox(
            "Frequency",
            ["", "Q", "Q,Y", "Q,HY,Y", "HY", "Y", "CM Only"],
            key=f"ppm_freq_sel_{counter}",
        )

    # ---------- month selection ----------
    st.markdown("#### Months")
    current_year = datetime.now().year
    current_month = datetime.now().month

    year_sel = st.selectbox(
        "Year",
        [current_year - 1, current_year, current_year + 1],
        index=1,
        key="ppm_year_sel",
    )

    month_short = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    selected_months: list[int] = []
    for row_start in range(0, 12, 6):
        month_cols = st.columns(6)
        for offset in range(6):
            m_idx = row_start + offset
            if m_idx >= 12:
                break
            month_num = m_idx + 1
            default_checked = (year_sel == current_year and month_num == current_month)
            with month_cols[offset]:
                checked = st.checkbox(
                    month_short[m_idx],
                    value=default_checked,
                    key=f"ppm_month_{year_sel}_{month_num}",
                )
                if checked:
                    selected_months.append(month_num)

    if not selected_months:
        st.warning("Select at least one month.")

    # ---------- save / refresh ----------
    st.write("")
    save_col, refresh_col = st.columns([2, 2])
    with save_col:
        save_now = st.button("💾 Save now", key="ppm_save_btn", type="primary")
    with refresh_col:
        if st.button("🔄 Refresh schedule fields", key="ppm_refresh_btn"):
            with st.spinner("Recomputing last done / next due / current PPM…"):
                all_ppms = db.fetch("ppm_schedule")
                seen = set()
                for p in all_ppms:
                    key = (
                        (p.get("asset_name") or "").strip(),
                        (p.get("contract_no") or "").strip(),
                        (p.get("venue") or "").strip(),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        _update_ppm_schedule_fields(db, key[0], key[1], key[2])
                    except Exception as exc:
                        st.warning(f"Update failed for {key[0]}: {exc}")
            st.success(f"Refreshed {len(seen)} asset schedule(s).")
            st.rerun()

    if save_now:
        if not contract_sel:
            st.error("Select a contract first.")
        elif not freq:
            st.error("Select a frequency.")
        elif not selected_months:
            st.error("Select at least one month.")
        elif not filtered_assets:
            st.error(
                "No assets match the current filters. "
                "Assign assets to this contract in **Asset Register** first."
            )
        else:
            existing_ppms = db.fetch("ppm_schedule")
            existing_set = {
                (
                    (p.get("asset_name") or "").strip(),
                    (p.get("contract_no") or "").strip(),
                    (p.get("venue") or "").strip(),
                    (p.get("planned_date") or "")[:7],
                )
                for p in existing_ppms
            }
            created = 0
            skipped = 0

            for month_num in selected_months:
                month_key = f"{year_sel}-{month_num:02d}"
                if year_sel == current_year and month_num == current_month:
                    planned = datetime.now().strftime("%Y-%m-%d")
                else:
                    planned = f"{year_sel}-{month_num:02d}-01"

                for a in filtered_assets:
                    asset_name = (a.get("asset_name") or "").strip()
                    venue_val = venue_sel or (a.get("venue") or "").strip()
                    dedup_key = (asset_name, contract_no, venue_val, month_key)
                    if dedup_key in existing_set:
                        skipped += 1
                        continue
                    record = {
                        "ppm_no": f"PPM-{asset_name}-{month_key}",
                        "asset_name": asset_name,
                        "contract_no": contract_no,
                        "venue": venue_val,
                        "frequency": freq,
                        "planned_date": planned,
                        "status": "Scheduled",
                        "qty": 1,
                    }
                    db.insert("ppm_schedule", record)
                    created += 1
                    existing_set.add(dedup_key)

            touched = set()
            for a in filtered_assets:
                touched.add((
                    (a.get("asset_name") or "").strip(),
                    contract_no,
                    venue_sel or (a.get("venue") or "").strip(),
                ))
            for name, cno, v in touched:
                try:
                    _update_ppm_schedule_fields(db, name, cno, v)
                except Exception as exc:
                    st.warning(f"Could not refresh schedule for {name}: {exc}")

            msg = f"Created {created} PPM record(s) across {len(selected_months)} month(s)."
            if skipped:
                msg += f" Skipped {skipped} already-existing PPM(s)."
            st.success(msg)
            st.rerun()

    # ---------- display PPMs for selected months ----------
    st.divider()
    show_ppms = st.checkbox(
        "Show PPMs for selected months",
        value=True,
        key="ppm_show_ppms",
    )

    if show_ppms and selected_months:
        all_ppms = db.fetch("ppm_schedule")
        selected_month_keys = {f"{year_sel}-{m:02d}" for m in selected_months}

        monthly = []
        for p in all_ppms:
            if (p.get("planned_date") or "")[:7] not in selected_month_keys:
                continue
            if contract_no and (p.get("contract_no") or "") != contract_no:
                continue
            if venue_sel and (p.get("venue") or "") != venue_sel:
                continue
            if asset_sel and (p.get("asset_name") or "").strip() != asset_sel:
                continue
            monthly.append(p)

        monthly.sort(key=lambda p: (
            p.get("planned_date") or "",
            p.get("asset_name") or "",
        ))

        month_label = ", ".join(month_short[m - 1] for m in sorted(selected_months))
        st.markdown(
            f"#### PPMs — {year_sel} ({month_label}) — {len(monthly)} record(s)"
        )

        if monthly:
            cols_to_show = [
                "ppm_no", "asset_name", "contract_no", "venue",
                "frequency", "planned_date", "status",
                "current_ppm", "last_done", "next_due", "qty",
            ]
            df = pd.DataFrame(
                [{c: p.get(c) for c in cols_to_show} for p in monthly]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No PPMs match the current filter for the selected months.")

        st.markdown("##### Breakdown by month")
        breakdown = []
        for m in sorted(selected_months):
            month_key = f"{year_sel}-{m:02d}"
            in_month = [
                p for p in monthly
                if (p.get("planned_date") or "")[:7] == month_key
            ]
            breakdown.append({
                "Month": f"{month_short[m-1]} {year_sel}",
                "Records": len(in_month),
                "Quantity": sum((p.get("qty") or 1) for p in in_month),
            })
        st.dataframe(pd.DataFrame(breakdown), use_container_width=True, hide_index=True)

        total_records = len(monthly)
        total_qty = sum((p.get("qty") or 1) for p in monthly)
        c1, c2 = st.columns(2)
        c1.metric("Total PPM records", total_records)
        c2.metric("Total quantity", total_qty)
    elif show_ppms:
        st.info("Select at least one month above to see PPMs.")

    # ---------- assets under contract ----------
    st.divider()
    st.markdown("#### Assets under contract")
    if not contract_sel:
        st.info("Select a contract to see assets.")
    else:
        scope_bits = [contract_sel]
        if venue_sel:
            scope_bits.append(f"venue: {venue_sel}")
        if asset_sel:
            scope_bits.append(f"asset: {asset_sel}")
        st.caption(f"{len(filtered_assets)} asset(s) — " + " · ".join(scope_bits))
        if filtered_assets:
            asset_cols = [
                c for c in (
                    "asset_name", "category", "qty", "venue", "contract_no", "status",
                )
                if any(c in a for a in filtered_assets)
            ]
            df = pd.DataFrame(
                [{c: a.get(c) for c in asset_cols} for a in filtered_assets]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info(
                "No assets match the current filters. "
                "Open **Asset Register** and set Contract No. / Venue on each asset."
            )

def render_ppm_dashboard(db: Database):
    st.subheader("PPM Dashboard")
    st.caption("At-a-glance PPM status by contract, venue and month.")

    contracts = db.fetch("contracts")
    contract_nos = sorted({
        (c.get("contract_no") or "").strip()
        for c in contracts
        if (c.get("contract_no") or "").strip()
    })

    all_assets = db.fetch("asset_register")
    all_ppms = db.fetch("ppm_schedule")
    current_year = datetime.now().year
    current_month = datetime.now().month
    current_month_key = f"{current_year}-{current_month:02d}"

    col_a, col_b, col_c = st.columns([2, 2, 1])
    with col_a:
        contract_no = st.selectbox(
            "Contract", [""] + contract_nos, key="dash_contract"
        )
    with col_b:
        if contract_no:
            contract_assets = [
                a for a in all_assets if (a.get("contract_no") or "") == contract_no
            ]
        else:
            contract_assets = all_assets
        venues = sorted({
            (a.get("venue") or "").strip()
            for a in contract_assets
            if (a.get("venue") or "").strip()
        })
        venue_sel = st.selectbox("Venue", [""] + venues, key="dash_venue")
    with col_c:
        year_sel = st.selectbox(
            "Year",
            [current_year - 1, current_year, current_year + 1],
            index=1,
            key="dash_year",
        )

    if not contract_no:
        st.info("Select a contract to view its PPM dashboard.")
        return

    # Filter PPMs to this contract / venue / year
    year_ppms = [
        p for p in all_ppms
        if (p.get("contract_no") or "") == contract_no
        and (p.get("planned_date") or "")[:4] == str(year_sel)
    ]
    if venue_sel:
        year_ppms = [
            p for p in year_ppms if (p.get("venue") or "").strip() == venue_sel
        ]

    # ---------- summary metrics ----------
    today_str = datetime.now().strftime("%Y-%m-%d")
    this_month_ppms = [
        p for p in year_ppms
        if (p.get("planned_date") or "")[:7] == current_month_key
    ] if year_sel == current_year else []

    completed_this_month = sum(
        1 for p in this_month_ppms if (p.get("status") or "") == "Completed"
    )
    overdue = sum(
        1 for p in year_ppms
        if (p.get("planned_date") or "") < today_str
        and (p.get("status") or "") != "Completed"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total PPMs this year", len(year_ppms))
    m2.metric("This month", len(this_month_ppms))
    m3.metric("Completed this month", completed_this_month)
    m4.metric("Overdue", overdue)

    st.divider()

    tab_month, tab_year = st.tabs(["📅 Single month", "📊 Year matrix"])

    # ---------- tab 1: single month ----------
    with tab_month:
        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        default_idx = current_month - 1 if year_sel == current_year else 0
        month_idx = st.selectbox(
            "Month",
            range(12),
            index=default_idx,
            format_func=lambda i: month_labels[i],
            key="dash_month",
        )
        month_num = month_idx + 1
        month_key = f"{year_sel}-{month_num:02d}"

        month_ppms = [
            p for p in year_ppms
            if (p.get("planned_date") or "")[:7] == month_key
        ]
        month_ppms.sort(key=lambda p: (
            (p.get("venue") or ""),
            (p.get("asset_name") or ""),
        ))

        status_filter = st.radio(
            "Status filter",
            ["All", "Pending", "Completed", "Overdue"],
            horizontal=True,
            key="dash_status",
        )
        if status_filter == "Completed":
            month_ppms = [
                p for p in month_ppms if (p.get("status") or "") == "Completed"
            ]
        elif status_filter == "Pending":
            month_ppms = [
                p for p in month_ppms if (p.get("status") or "") != "Completed"
            ]
        elif status_filter == "Overdue":
            month_ppms = [
                p for p in month_ppms
                if (p.get("planned_date") or "") < today_str
                and (p.get("status") or "") != "Completed"
            ]

        # Per-venue summary when no venue is selected
        if not venue_sel and month_ppms:
            st.markdown("##### Per-venue breakdown")
            venue_summary = {}
            for p in month_ppms:
                v = (p.get("venue") or "").strip() or "—"
                if v not in venue_summary:
                    venue_summary[v] = {
                        "Venue": v,
                        "Assets": set(),
                        "Completed": 0,
                        "Pending": 0,
                        "Overdue": 0,
                    }
                venue_summary[v]["Assets"].add((p.get("asset_name") or "").strip())
                if (p.get("status") or "") == "Completed":
                    venue_summary[v]["Completed"] += 1
                elif (p.get("planned_date") or "") < today_str:
                    venue_summary[v]["Overdue"] += 1
                else:
                    venue_summary[v]["Pending"] += 1
            summary_rows = []
            for v in sorted(venue_summary.keys()):
                info = venue_summary[v]
                summary_rows.append({
                    "Venue": v,
                    "Assets": len(info["Assets"]),
                    "Completed": info["Completed"],
                    "Pending": info["Pending"],
                    "Overdue": info["Overdue"],
                })
            st.dataframe(
                pd.DataFrame(summary_rows),
                use_container_width=True,
                hide_index=True,
            )

        st.markdown(f"##### {month_labels[month_idx]} {year_sel} — {len(month_ppms)} PPM(s)")

        if month_ppms:
            rows = []
            for p in month_ppms:
                rows.append({
                    "Asset": p.get("asset_name") or "",
                    "Venue": p.get("venue") or "",
                    "Frequency": p.get("frequency") or "",
                    "Planned": p.get("planned_date") or "",
                    "Last Done": p.get("last_done") or "—",
                    "Next Due": p.get("next_due") or "—",
                    "Current PPM": p.get("current_ppm") or "",
                    "Status": p.get("status") or "",
                })
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No PPMs match this month with the current filters.")

    # ---------- tab 2: year matrix ----------
    with tab_year:
        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        matrix_assets = sorted({
            (p.get("asset_name") or "").strip()
            for p in year_ppms
            if (p.get("asset_name") or "").strip()
        })

        if not matrix_assets:
            st.info("No PPMs for this contract in the selected year.")
        else:
            matrix_rows = []
            for asset in matrix_assets:
                asset_ppms = [
                    p for p in year_ppms
                    if (p.get("asset_name") or "").strip() == asset
                ]
                row = {"Asset": asset}
                if not venue_sel:
                    asset_venue = (
                        (asset_ppms[0].get("venue") or "").strip()
                        if asset_ppms else ""
                    )
                    row["Venue"] = asset_venue or "—"
                for m in range(1, 13):
                    mk = f"{year_sel}-{m:02d}"
                    in_month = [
                        p for p in asset_ppms
                        if (p.get("planned_date") or "")[:7] == mk
                    ]
                    if not in_month:
                        row[month_labels[m - 1]] = ""
                        continue
                    statuses = {(p.get("status") or "") for p in in_month}
                    if "Completed" in statuses:
                        row[month_labels[m - 1]] = "✓"
                    elif (
                        year_sel == current_year and m == current_month
                    ):
                        row[month_labels[m - 1]] = "★"
                    elif any(
                        (p.get("planned_date") or "") < today_str
                        for p in in_month
                    ):
                        row[month_labels[m - 1]] = "⚠"
                    else:
                        row[month_labels[m - 1]] = "○"
                matrix_rows.append(row)

            st.dataframe(
                pd.DataFrame(matrix_rows),
                use_container_width=True,
                hide_index=True,
            )
            st.caption("✓ Completed   ★ Due this month   ⚠ Overdue   ○ Scheduled")

# ---------------------------------------------------------------------------
# App shell
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    db = get_db()

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
        st.markdown("## LijiArts Ltd")
        st.caption("Facility Management — DB: Turso (persistent)")

    tab_labels = [m["name"] for m in MODULES]
    sv_idx = next(i for i, m in enumerate(MODULES) if m["table"] == "sv_stock_voucher")
    tab_labels.insert(sv_idx + 1, "Stock Balance")
    ppm_label = next(m["name"] for m in MODULES if m["table"] == "ppm_schedule")
    ppm_pos = tab_labels.index(ppm_label)
    tab_labels.insert(ppm_pos + 1, "PPM Generator")
    tab_labels.insert(ppm_pos + 2, "PPM Dashboard")

    with st.sidebar:
        st.header("Modules")
        selected_tab = st.radio(
            "Select module",
            tab_labels,
            label_visibility="collapsed",
        )

    if selected_tab == "Stock Balance":
        render_stock_balance(db)
    elif selected_tab == "PPM Generator":
        render_ppm_generator(db)
    elif selected_tab == "PPM Dashboard":
        render_ppm_dashboard(db)
    else:
        module = next(m for m in MODULES if m["name"] == selected_tab)
        render_module(module, db)

if __name__ == "__main__":
    main()
