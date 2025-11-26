from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import os, json, datetime, math, uuid, random

# --- Added imports for upload/parsing ---
import io, csv, re
try:
    import openpyxl
    _HAS_OPENPYXL = True
except Exception:
    _HAS_OPENPYXL = False

# DB abstraction: uses sqlite if DATABASE_URL not set; otherwise tries psycopg2 for Postgres
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("INTERNAL_DATABASE_URL")

USE_POSTGRES = False
pg = None
try:
    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        import psycopg2
        import psycopg2.extras as pgextras
        USE_POSTGRES = True
        pg = psycopg2
except Exception:
    # psycopg2 may not be installed in the environment; we'll fall back to sqlite
    USE_POSTGRES = False

if not USE_POSTGRES:
    import sqlite3
    DB = os.path.join(os.path.dirname(__file__), "payroll.db")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "change_this_secret_" + str(uuid.uuid4()))

# ---------- Utilities ----------

def get_db():
    """Return a DB connection. For sqlite3 we set row_factory to sqlite3.Row.
    For Postgres we return a psycopg2 connection and use DictCursor where needed.
    """
    if USE_POSTGRES:
        conn = pg.connect(DATABASE_URL)
        return conn
    else:
        conn = sqlite3.connect(DB, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        conn.row_factory = sqlite3.Row
        return conn


def fetchone_dict(cur):
    """Return one row as a dict from a cursor depending on DB type."""
    if USE_POSTGRES:
        row = cur.fetchone()
        return dict(row) if row else None
    else:
        return cur.fetchone()


def fetchall_dict(cur):
    if USE_POSTGRES:
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    else:
        return cur.fetchall()


def init_db():
    """Run schema.sql on the target DB and ensure admin exists. Adds new columns to timecards if missing.
    """
    schema = open("schema.sql").read()
    if USE_POSTGRES:
        conn = get_db()
        cur = conn.cursor()
        # run schema statements (best-effort)
        for stmt in schema.split(";"):
            s = stmt.strip()
            if not s:
                continue
            try:
                cur.execute(s + ";")
            except Exception:
                pass
        # ensure admin
        username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "abic123")
        password_hash = generate_password_hash(password)
        try:
            cur.execute("INSERT INTO admins (id, username, password_hash) VALUES (%s,%s,%s) ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username, password_hash = EXCLUDED.password_hash", (1, username, password_hash))
        except Exception:
            try:
                cur.execute("UPDATE admins SET username=%s, password_hash=%s WHERE id=1", (username, password_hash))
            except Exception:
                pass
        # attempt migration for timecards
        try:
            cur.execute("ALTER TABLE timecards ADD COLUMN is_regular_holiday BOOLEAN DEFAULT FALSE")
            cur.execute("ALTER TABLE timecards ADD COLUMN is_special_holiday BOOLEAN DEFAULT FALSE")
        except Exception:
            # columns may already exist
            pass
        conn.commit()
        conn.close()
    else:
        conn = get_db()
        cur = conn.cursor()
        cur.executescript(schema)
        username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "abic123")
        password_hash = generate_password_hash(password)
        try:
            cur.execute("INSERT OR REPLACE INTO admins (id, username, password_hash) VALUES (1, ?, ?)", (username, password_hash))
        except Exception:
            cur.execute("DELETE FROM admins WHERE id=1")
            cur.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, ?, ?)", (username, password_hash))
        # migrate timecards table: add holiday columns if missing
        try:
            cur.execute("ALTER TABLE timecards ADD COLUMN is_regular_holiday INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE timecards ADD COLUMN is_special_holiday INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.commit()
        conn.close()


# ---------- Helpers ----------

def randpass(n=8):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(random.choice(chars) for _ in range(n))


def make_username(name):
    base = "".join(c for c in name.lower() if c.isalnum())
    return (base[:8] + str(random.randint(10,99)))


# ---------- Time helpers ----------

def to_minutes(tstr):
    if not tstr or str(tstr).strip() == "":
        return None
    try:
        # normalize NBSP and similar
        s = re.sub(r'\s+', ' ', str(tstr)).strip()
        s = s.replace('\u202f', '').replace('\xa0', '').strip()
        # handle possible AM/PM
        m = re.match(r'(\d{1,2}):(\d{2})(?:\s*([AaPp]\.?[Mm]\.?))?', s)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2))
            ampm = m.group(3)
            if ampm:
                ampm = ampm.lower().replace('.', '')
                if ampm.startswith('p') and h < 12:
                    h += 12
                if ampm.startswith('a') and h == 12:
                    h = 0
            return h*60 + mi
        # fallback: handle HHMM or HMM
        m2 = re.match(r'^(\d{1,2})(\d{2})$', re.sub(r'[:\s]', '', s))
        if m2:
            h = int(m2.group(1)); mi = int(m2.group(2))
            return h*60 + mi
        # else fail
        return None
    except Exception:
        return None


def overlap_minutes(start1, end1, start2, end2):
    s = max(start1, start2)
    e = min(end1, end2)
    return max(0, e - s)


# ---------- Timecard aggregator (Option A absence detection) ----------

def daterange(start_date, end_date):
    d = start_date
    while d <= end_date:
        yield d
        d += datetime.timedelta(days=1)


def compute_timecard_hours(emp_row, conn, start_date_str, end_date_str):
    # emp_row is a mapping-like object; use [] access
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute("SELECT date, time_in, time_out, COALESCE(is_regular_holiday,false) AS is_regular_holiday, COALESCE(is_special_holiday,false) AS is_special_holiday FROM timecards WHERE employee_id=%s AND date BETWEEN %s AND %s ORDER BY date ASC", (emp_row["id"], start_date_str, end_date_str))
    else:
        cur.execute("SELECT date, time_in, time_out, COALESCE(is_regular_holiday,0) AS is_regular_holiday, COALESCE(is_special_holiday,0) AS is_special_holiday FROM timecards WHERE employee_id=? AND date BETWEEN ? AND ? ORDER BY date ASC", (emp_row["id"], start_date_str, end_date_str))
    rows = fetchall_dict(cur)
    timecard_map = {r["date"]: r for r in rows}

    worked_minutes = 0
    regular_ot_minutes = 0
    restday_minutes = 0
    restday_ot_minutes = 0
    night_diff_minutes = 0
    nsd_ot_minutes = 0
    lwop_days = 0
    tardiness_minutes_total = 0
    undertime_minutes_total = 0
    present_days = 0
    absent_days = 0

    # also collect holiday hours totals for payroll breakdown
    special_hol_minutes = 0
    regular_hol_minutes = 0

    rest_day_name = (emp_row.get("rest_day") or "").strip().lower() if isinstance(emp_row, dict) else (emp_row["rest_day"] or "").strip().lower()
    weekday_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    rest_weekday = weekday_map.get(rest_day_name, None)

    WORK_START = 9 * 60
    WORK_END = 18 * 60
    OT_THRESHOLD = 19 * 60
    STD_WORK_MINUTES = 8 * 60

    start_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()

    for single_date in daterange(start_date, end_date):
        date_str = single_date.strftime("%Y-%m-%d")
        weekday = single_date.weekday()
        is_restday = (rest_weekday is not None and weekday == rest_weekday)

        tc = timecard_map.get(date_str)
        if not tc:
            if is_restday:
                continue
            else:
                absent_days += 1
                continue
        present_days += 1
        time_in = tc["time_in"]
        time_out = tc["time_out"]

        min_in = to_minutes(time_in)
        min_out = to_minutes(time_out)
        if min_in is None or min_out is None:
            lwop_days += 1
            absent_days += 1
            present_days -= 1
            continue

        if min_out <= min_in:
            min_out += 24*60

        total_minutes = min_out - min_in

        tardiness_min = max(0, min_in - WORK_START)
        tardiness_minutes_total += tardiness_min

        undertime_min = 0
        if min_out < WORK_END:
            undertime_min = max(0, WORK_END - min_out)
        undertime_minutes_total += undertime_min

        ot_min = max(0, min_out - OT_THRESHOLD)

        nd1_s, nd1_e = 22*60, 24*60
        nd2_s, nd2_e = 0, 6*60
        nd_minutes = overlap_minutes(min_in, min_out, nd1_s, nd1_e) + overlap_minutes(min_in, min_out, nd2_s + 24*60, nd2_e + 24*60)

        regular_part = min(total_minutes, STD_WORK_MINUTES)

        # holiday flags (db stores truthy values)
        is_regular_holiday = bool(tc.get("is_regular_holiday") if isinstance(tc, dict) else tc["is_regular_holiday"])
        is_special_holiday = bool(tc.get("is_special_holiday") if isinstance(tc, dict) else tc["is_special_holiday"])

        # if holiday, count separately (they still count as present for absence logic)
        if is_regular_holiday:
            regular_hol_minutes += regular_part
        elif is_special_holiday:
            special_hol_minutes += regular_part
        elif is_restday:
            restday_minutes += regular_part
            restday_ot_minutes += ot_min
        else:
            worked_minutes += regular_part
            regular_ot_minutes += ot_min

        if nd_minutes > 0:
            night_diff_minutes += nd_minutes
            if ot_min > 0:
                ot_start = OT_THRESHOLD
                nsd_ot = overlap_minutes(ot_start, min_out, nd1_s, nd1_e) + overlap_minutes(ot_start, min_out, nd2_s + 24*60, nd2_e + 24*60)
                nsd_ot_minutes += nsd_ot

    def h(m): return round((m or 0)/60.0, 2)
    return {
        "present_days": present_days,
        "absent_days": absent_days,
        "worked_hours": h(worked_minutes),
        "regular_ot_hours": h(regular_ot_minutes),
        "restday_hours": h(restday_minutes),
        "restday_ot_hours": h(restday_ot_minutes),
        "night_diff_hours": h(night_diff_minutes),
        "nsd_ot_hours": h(nsd_ot_minutes),
        "lwop_days": lwop_days,
        "tardiness_hours": h(tardiness_minutes_total),
        "undertime_hours": h(undertime_minutes_total),
        # holiday breakdown in hours
        "special_hol_hours": h(special_hol_minutes),
        "regular_hol_hours": h(regular_hol_minutes)
    }


# ---------- Payroll logic ----------
# ... (compute_tax and compute_payroll unchanged) ...

def compute_tax(monthly_taxable):
    t = float(monthly_taxable)
    tax = 0.0
    if t <= 20833:
        tax = 0.0
    elif t <= 33333:
        tax = 0.0
    elif t <= 66667:
        tax = 0.0
    elif t <= 166667:
        tax = 0.15 * (t - 20833)
    elif t <= 666667:
        tax = 1875 + 0.20 * (t - 33333)
    else:
        tax = 8541.80 + 0.25 * (t - 66667)
    return round(tax,2)


def compute_payroll(employee, inputs):
    monthly = float(employee["monthly_base_pay"] or 0)
    daily_rate = float(inputs.get("daily_rate",0)) or (monthly/313*12 if monthly>0 else 0)
    hourly_rate = float(inputs.get("hourly_rate",0)) or (daily_rate/8 if daily_rate>0 else 0)
    def money(x): return round((float(x) if x is not None else 0.0) + 0.0000001,2)

    present_days = int(inputs.get("present_days",0))
    absent_days = int(inputs.get("absent_days",0))

    # regular earnings based on days present (excluding holiday premiums - those are added below)
    regular_earnings = money(daily_rate * present_days)
    absence_deduction = money(daily_rate * absent_days)

    hrs = {k: float(inputs.get(k,0) or 0) for k in ["worked_hours","regular_ot_hours","restday_hours","restday_ot_hours","night_diff_hours","nsd_ot_hours","special_hol_hours","regular_hol_hours","regular_hol_ot_hours"]}
    earnings = {}
    earnings["regular_pay"] = regular_earnings
    earnings["regular_ot"] = money(hourly_rate * 1.25 * hrs["regular_ot_hours"])
    earnings["restday"] = money(hourly_rate * 1.30 * hrs["restday_hours"])
    earnings["restday_ot"] = money(hourly_rate * 1.69 * hrs["restday_ot_hours"])
    earnings["night_diff"] = money(hourly_rate * 1.10 * hrs["night_diff_hours"])
    earnings["nsd_ot"] = money(hourly_rate * 1.375 * hrs["nsd_ot_hours"])
    # Holiday premiums: pay the holiday rate FOR THE HOURS worked on that holiday
    # Regular holiday: 200% of hourly -> effectively hourly * 2.0 * hours (we treat as premium replacing regular pay for those hours)
    earnings["regular_holiday"] = money(hourly_rate * 2.00 * hrs.get("regular_hol_hours",0))
    # Special holiday: 130%
    earnings["special_holiday"] = money(hourly_rate * 1.30 * hrs.get("special_hol_hours",0))
    # Regular holiday OT (if any)
    earnings["regular_holiday_ot"] = money(hourly_rate * 2.60 * hrs.get("regular_hol_ot_hours",0))

    incentives = float(inputs.get("incentives",0) or 0)
    earnings["incentives"] = money(incentives)

    total_earnings = money(sum(v for v in earnings.values()))

    tardiness_hours = float(inputs.get("tardiness_hours",0) or 0)
    undertime_hours = float(inputs.get("undertime_hours",0) or 0)
    lwop_days = float(inputs.get("lwop_days",0) or 0)
    sss_loan = float(inputs.get("sss_loan",0) or 0)
    pagibig_loan = float(inputs.get("pagibig_loan",0) or 0)

    tardiness_amt = hourly_rate * tardiness_hours
    undertime_amt = hourly_rate * undertime_hours
    lwop_amt = daily_rate * lwop_days

    apply_sss = inputs.get("apply_sss") in ["1","true","on","yes",True]
    apply_phil = inputs.get("apply_phil") in ["1","true","on","yes",True]
    apply_pagibig = inputs.get("apply_pagibig") in ["1","true","on","yes",True]

    sss = money(monthly * 0.05) if apply_sss else 0.0
    phil = money(monthly * 0.025) if apply_phil else 0.0
    pagibig = 200.0 if apply_pagibig else 0.0

    gross_pay = money(regular_earnings + earnings.get("regular_ot",0) + earnings.get("restday",0) + earnings.get("restday_ot",0) + earnings.get("night_diff",0) + earnings.get("nsd_ot",0) + earnings.get("special_holiday",0) + earnings.get("regular_holiday",0) + earnings.get("regular_holiday_ot",0) + earnings.get("incentives",0) - absence_deduction - tardiness_amt - undertime_amt - lwop_amt)

    subtotal = gross_pay
    taxable = max(0, subtotal - (sss + phil + (pagibig if apply_pagibig else 0)))
    tax = compute_tax(taxable)

    total_deductions = money(tardiness_amt + undertime_amt + lwop_amt + (sss if sss else 0) + (phil if phil else 0) + (pagibig if apply_pagibig else 0) + sss_loan + pagibig_loan + tax + absence_deduction)

    net = money(gross_pay - ((sss if sss else 0) + (phil if phil else 0) + (pagibig if apply_pagibig else 0) + tax + sss_loan + pagibig_loan))

    result = {
        "rates": {"monthly": money(monthly), "daily_rate": money(daily_rate), "hourly_rate": money(hourly_rate)},
        "earnings": earnings,
        "regular_earnings": regular_earnings,
        "absence_deduction": absence_deduction,
        "total_earnings": money(total_earnings),
        "gross_pay": gross_pay,
        "deductions": {"tardiness": money(tardiness_amt), "undertime": money(undertime_amt), "lwop": money(lwop_amt), "sss": sss, "philhealth": phil, "pagibig": pagibig, "sss_loan": money(sss_loan), "pagibig_loan": money(pagibig_loan), "tax": money(tax)},
        "total_deductions": total_deductions,
        "net": net,
        "inputs": inputs
    }
    return result


# ---------- Bulk upload helpers & route ----------

def try_parse_date(value):
    """Try multiple date formats and also accept Excel datetime objects."""
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    if s == "":
        return None
    # common formats
    fmts = ["%m/%d/%Y","%m/%d/%y","%Y-%m-%d","%d/%m/%Y","%d-%m-%Y"]
    for f in fmts:
        try:
            return datetime.datetime.strptime(s, f).date()
        except Exception:
            pass
    # try parsing with month names
    fmts2 = ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"]
    for f in fmts2:
        try:
            return datetime.datetime.strptime(s, f).date()
        except Exception:
            pass
    # last attempt: try to parse mm/dd with year inference (not ideal)
    try:
        parts = re.split(r'[\/\-\.]', s)
        if len(parts) >= 3:
            m = int(parts[0]); d = int(parts[1]); y = int(parts[2])
            if y < 100:
                y += 2000
            return datetime.date(y, m, d)
    except Exception:
        pass
    return None

def normalize_time_str(s):
    if s is None:
        return None
    return str(s).replace('\u202f', '').replace('\xa0','').strip()

@app.route("/admin/timecards/upload", methods=["GET","POST"])
def admin_timecards_upload():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    if request.method == "GET":
        # Simple upload form template — you can create 'admin_timecards_upload.html' to show a nicer UI.
        return render_template("admin_timecards_upload.html")
    # POST: process file
    f = request.files.get("file")
    if not f:
        flash("No file uploaded", "danger")
        return redirect(url_for("admin_dashboard"))
    filename = f.filename or "upload"
    data = f.read()
    inserted = 0
    skipped = 0
    parse_errors = 0
    unknown_employees = []
    conn = get_db(); cur = conn.cursor()

    # helper to find employee by many possible fields
    def find_employee_by_identifier(identifier):
        """Try to find employee by numeric id, employee_code, username, or name (best-effort)."""
        if identifier is None:
            return None
        s = str(identifier).strip()
        if s == "":
            return None
        # Try numeric id
        try:
            nid = int(s)
            cur.execute("SELECT * FROM employees WHERE id=%s" if USE_POSTGRES else "SELECT * FROM employees WHERE id=?", (nid,))
            row = fetchone_dict(cur)
            if row:
                return row
        except Exception:
            pass
        # Try employee_code-like columns
        EMPLOYEE_CODE_FIELDS = ("employee_code","emp_code","code")
        for field in EMPLOYEE_CODE_FIELDS:
            try:
                cur.execute(f"SELECT * FROM employees WHERE {field}=%s" if USE_POSTGRES else f"SELECT * FROM employees WHERE {field}=?", (s,))
                row = fetchone_dict(cur)
                if row:
                    return row
            except Exception:
                # column might not exist; ignore
                pass
        # Try username
        try:
            cur.execute("SELECT * FROM employees WHERE username=%s" if USE_POSTGRES else "SELECT * FROM employees WHERE username=?", (s,))
            row = fetchone_dict(cur)
            if row:
                return row
        except Exception:
            pass
        # Try matching by name (case-insensitive, partial)
        try:
            if USE_POSTGRES:
                cur.execute("SELECT * FROM employees WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{s.lower()}%",))
            else:
                cur.execute("SELECT * FROM employees WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{s.lower()}%",))
            row = fetchone_dict(cur)
            if row:
                return row
        except Exception:
            pass
        return None

    # detect file type: xlsx if starts with PK? or filename endswith .xlsx
    processed_rows = []
    try:
        is_xlsx = (filename.lower().endswith(".xlsx") or filename.lower().endswith(".xls")) and _HAS_OPENPYXL
        if is_xlsx:
            # use openpyxl to read
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                # Expect columns: employee_id, name(optional), date, time_in, time_out
                if not row or all([c is None or str(c).strip()=="" for c in row]):
                    continue
                processed_rows.append([c for c in row])
        else:
            # treat as CSV/TSV/text
            txt = None
            try:
                txt = data.decode("utf-8-sig")
            except Exception:
                try:
                    txt = data.decode("latin-1")
                except Exception:
                    txt = data.decode("utf-8", errors="ignore")
            # guess delimiter: tab if '\t' exists in first 1024 bytes else comma
            sample = txt[:4096]
            delim = '\t' if '\t' in sample and sample.count('\t') >= sample.count(',') else ','
            reader = csv.reader(io.StringIO(txt), delimiter=delim)
            for row in reader:
                if not row or all([c is None or str(c).strip()=="" for c in row]):
                    continue
                processed_rows.append([c for c in row])
    except Exception as e:
        conn.close()
        flash(f"Failed to parse file: {e}", "danger")
        return redirect(url_for("admin_dashboard"))

    # Process each parsed row
    row_no = 0
    for raw in processed_rows:
        row_no += 1
        # accept shortest patterns (employee_id, date, time_in, time_out) or (employee_id, name, date, time_in, time_out)
        try:
            # normalize fields
            if len(raw) >= 5:
                identifier = raw[0]
                # name = raw[1]  # ignored for matching
                date_val = raw[2]
                time_in_val = normalize_time_str(raw[3])
                time_out_val = normalize_time_str(raw[4])
            elif len(raw) == 4:
                identifier = raw[0]
                date_val = raw[1]
                time_in_val = normalize_time_str(raw[2])
                time_out_val = normalize_time_str(raw[3])
            else:
                # cannot parse
                parse_errors += 1
                continue

            # parse date
            parsed_date = try_parse_date(date_val)
            if parsed_date is None:
                parse_errors += 1
                continue
            date_str = parsed_date.strftime("%Y-%m-%d")

            # parse times to normalize HH:MM (store as strings 'HH:MM')
            tin_min = to_minutes(time_in_val)
            tout_min = to_minutes(time_out_val)
            tin_store = None
            tout_store = None
            if tin_min is not None:
                tin_store = f"{tin_min//60:02d}:{tin_min%60:02d}"
            if tout_min is not None:
                tout_store = f"{tout_min//60%24:02d}:{tout_min%60:02d}"

            # find employee
            emp = find_employee_by_identifier(identifier)
            if not emp:
                unknown_employees.append(str(identifier))
                skipped += 1
                continue
            emp_id = emp["id"]

            # Insert into timecards table (respecting DB flavor)
            try:
                if USE_POSTGRES:
                    cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out,is_regular_holiday,is_special_holiday) VALUES (%s,%s,%s,%s,%s,%s)",
                                (emp_id, date_str, tin_store, tout_store, False, False))
                else:
                    cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out,is_regular_holiday,is_special_holiday) VALUES (?,?,?,?,?,?)",
                                (emp_id, date_str, tin_store, tout_store, 0, 0))
                conn.commit()
                inserted += 1
            except Exception as e:
                # if insert fails, try a simpler insert without holiday cols (for schema mismatch)
                try:
                    if USE_POSTGRES:
                        cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out) VALUES (%s,%s,%s,%s)", (emp_id, date_str, tin_store, tout_store))
                    else:
                        cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out) VALUES (?,?,?,?)", (emp_id, date_str, tin_store, tout_store))
                    conn.commit()
                    inserted += 1
                except Exception:
                    parse_errors += 1
                    continue
        except Exception:
            parse_errors += 1
            continue

    conn.close()
    msg = f"Upload complete — inserted={inserted}, skipped={skipped}, parse_errors={parse_errors}"
    if unknown_employees:
        msg += f"; unknown_employees={len(unknown_employees)} (examples: {', '.join(unknown_employees[:5])})"
    flash(msg, "success")
    return redirect(url_for("admin_dashboard"))


# ---------- Routes ----------
@app.before_first_request
def startup():
    try:
        init_db()
    except Exception as e:
        print("init_db error:", e)


@app.route("/")
def index():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    if session.get("employee_id"):
        return redirect(url_for("employee_home"))
    return render_template("index.html")


# ... rest of your routes unchanged (admin login, employee login, admin_timecards etc.) ...
# I left the rest of the file's routes and handlers as in your provided code; they remain unchanged.

# Note: The original handlers (admin_timecards, admin_generate_payroll, admin_add_employee, etc.) are still present below.
# (file continues unchanged)

# for brevity in this paste, ensure you keep the remainder of your app.py unchanged, including:
# - admin/login/logout
# - employee/login/logout
# - admin_dashboard
# - admin_add_employee
# - admin_timecards (the individual employee timecards page)
# - admin_generate_payroll
# - employee_home
# - payroll_view
# - and the usual __main__ startup
#
# The upload route above integrates with the same DB and timecards table and commits rows.

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=True)
