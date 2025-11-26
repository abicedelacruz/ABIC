from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import os, json, datetime, math, uuid, random

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
        # psycopg2 connects via DATABASE_URL
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
    """Run schema.sql on the target DB and ensure admin exists. Does not drop employees.
    If Postgres is used, execute statements splitting on ';' with care.
    """
    schema = open("schema.sql").read()
    if USE_POSTGRES:
        conn = get_db()
        cur = conn.cursor()
        # psycopg2 can execute multi-statement via executing the whole script using split by ';'
        # We'll execute each statement that has non-whitespace
        for stmt in schema.split(";"):
            s = stmt.strip()
            if not s:
                continue
            try:
                cur.execute(s + ";")
            except Exception:
                # ignore statements that may not be compatible; raise for visibility
                pass
        # create or replace admin
        username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "abic123")
        password_hash = generate_password_hash(password)
        # upsert pattern
        try:
            cur.execute("SELECT id FROM admins WHERE id=1")
            if cur.fetchone():
                cur.execute("UPDATE admins SET username=%s, password_hash=%s WHERE id=1", (username, password_hash))
            else:
                cur.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, %s, %s)", (username, password_hash))
        except Exception:
            # fallback: attempt insert
            try:
                cur.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, %s, %s)", (username, password_hash))
            except Exception:
                pass
        conn.commit()
        conn.close()
    else:
        conn = get_db()
        cur = conn.cursor()
        cur.executescript(schema)
        # reset admin only (do not delete employees)
        username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "abic123")
        password_hash = generate_password_hash(password)
        try:
            cur.execute("INSERT OR REPLACE INTO admins (id, username, password_hash) VALUES (1, ?, ?)", (username, password_hash))
        except Exception:
            # older sqlite may not support INSERT OR REPLACE with the schema; use delete/insert
            cur.execute("DELETE FROM admins WHERE id=1")
            cur.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, ?, ?)", (username, password_hash))
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
        h,m = map(int, str(tstr).split(":"))
        return h*60 + m
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
    cur.execute("SELECT date, time_in, time_out FROM timecards WHERE employee_id=%s AND date BETWEEN %s AND %s ORDER BY date ASC" if USE_POSTGRES else "SELECT date, time_in, time_out FROM timecards WHERE employee_id=? AND date BETWEEN ? AND ? ORDER BY date ASC",
                (emp_row["id"], start_date_str, end_date_str))
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

        if is_restday:
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
        "undertime_hours": h(undertime_minutes_total)
    }


# ---------- Payroll logic ----------

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

    regular_earnings = money(daily_rate * present_days)
    absence_deduction = money(daily_rate * absent_days)

    hrs = {k: float(inputs.get(k,0) or 0) for k in ["worked_hours","regular_ot_hours","restday_hours","restday_ot_hours","night_diff_hours","nsd_ot_hours","special_hol_hours","special_hol_rd_hours","regular_hol_hours","regular_hol_ot_hours"]}
    earnings = {}
    earnings["regular_pay"] = regular_earnings
    earnings["regular_ot"] = money(hourly_rate * 1.25 * hrs["regular_ot_hours"])
    earnings["restday"] = money(hourly_rate * 1.30 * hrs["restday_hours"])
    earnings["restday_ot"] = money(hourly_rate * 1.69 * hrs["restday_ot_hours"])
    earnings["night_diff"] = money(hourly_rate * 1.10 * hrs["night_diff_hours"])
    earnings["nsd_ot"] = money(hourly_rate * 1.375 * hrs["nsd_ot_hours"])
    earnings["special_holiday"] = money(hourly_rate * 1.30 * hrs.get("special_hol_hours",0))
    earnings["special_holiday_rd"] = money(hourly_rate * 1.50 * hrs.get("special_hol_rd_hours",0))
    earnings["regular_holiday"] = money(hourly_rate * 2.00 * hrs.get("regular_hol_hours",0))
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

    # IMPORTANT: Do NOT apply SSS/Phil/PagIBIG automatically. Only apply if inputs flags are truthy.
    apply_sss = inputs.get("apply_sss") in ["1","true","on","yes",True]
    apply_phil = inputs.get("apply_phil") in ["1","true","on","yes",True]
    apply_pagibig = inputs.get("apply_pagibig") in ["1","true","on","yes",True]

    sss = money(monthly * 0.05) if apply_sss else 0.0
    phil = money(monthly * 0.025) if apply_phil else 0.0
    pagibig = 200.0 if apply_pagibig else 0.0

    gross_pay = money(regular_earnings + earnings.get("regular_ot",0) + earnings.get("restday",0) + earnings.get("restday_ot",0) + earnings.get("night_diff",0) + earnings.get("nsd_ot",0) + earnings.get("special_holiday",0) + earnings.get("special_holiday_rd",0) + earnings.get("regular_holiday",0) + earnings.get("regular_holiday_ot",0) + earnings.get("incentives",0) - absence_deduction - tardiness_amt - undertime_amt - lwop_amt)

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


@app.route("/init_admin")
def init_admin():
    expected = os.environ.get("INIT_ADMIN_SECRET", "init_admin_secret")
    provided = request.args.get("secret", "")
    if provided != expected:
        return ("Forbidden: invalid secret. Set INIT_ADMIN_SECRET or provide correct secret in query string.", 403)
    username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
    password = os.environ.get("INITIAL_ADMIN_PASSWORD", "abic123")
    hashpw = generate_password_hash(password)
    conn = get_db(); cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute("INSERT INTO admins (id, username, password_hash) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username, password_hash = EXCLUDED.password_hash", (1, username, hashpw))
    else:
        cur.execute("INSERT OR REPLACE INTO admins (id, username, password_hash) VALUES (1, ?, ?)", (username, hashpw))
    conn.commit(); conn.close()
    return f"Admin user created/updated. Username: {username} Password: {password}. PLEASE REMOVE / DISABLE / CHANGE INIT_ADMIN_SECRET AFTER USE."


# Admin auth
@app.route("/admin/login", methods=["GET","POST"]) 
def admin_login():
    if request.method=="POST":
        u = request.form['username']; p = request.form['password']
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM admins WHERE username=%s" if USE_POSTGRES else "SELECT * FROM admins WHERE username=?", (u,))
        row = fetchone_dict(cur)
        conn.close()
        if row and check_password_hash(row["password_hash"], p):
            session["is_admin"] = True; session["admin_id"] = row["id"]
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials","danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear(); return redirect(url_for("index"))


# Employee auth
@app.route("/employee/login", methods=["GET","POST"]) 
def employee_login():
    if request.method=="POST":
        u = request.form['username']; p = request.form['password']
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM employees WHERE username=%s" if USE_POSTGRES else "SELECT * FROM employees WHERE username=?", (u,))
        row = fetchone_dict(cur)
        conn.close()
        if row and check_password_hash(row["password_hash"], p):
            session["employee_id"] = row["id"]; session["is_admin"] = False
            return redirect(url_for("employee_home"))
        flash("Invalid credentials","danger")
    return render_template("employee_login.html")


@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))


# Admin dashboard and employee management
@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees ORDER BY id DESC")
    employees = fetchall_dict(cur)
    conn.close()
    return render_template("admin_dashboard.html", employees=employees)


@app.route("/admin/employee/add", methods=["GET","POST"]) 
def admin_add_employee():
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    if request.method=="POST":
        name = request.form['name']; company = request.form['company']; rest_day = request.form['rest_day']
        monthly = float(request.form.get('monthly_base_pay') or 0)
        username = make_username(name); password = randpass(8)
        phash = generate_password_hash(password)
        conn = get_db(); cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("INSERT INTO employees (name, company, rest_day, monthly_base_pay, username, password_hash) VALUES (%s,%s,%s,%s,%s,%s)", (name, company, rest_day, monthly, username, phash))
        else:
            cur.execute("INSERT INTO employees (name, company, rest_day, monthly_base_pay, username, password_hash) VALUES (?,?,?,?,?,?)", (name, company, rest_day, monthly, username, phash))
        conn.commit(); emp_id = cur.lastrowid if not USE_POSTGRES else cur.fetchone()[0] if False else None
        conn.close()
        flash(f'Employee created. Username: {username} Password: {password} (copy these)', "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_add_employee.html")


@app.route("/admin/employee/<int:emp_id>/remove", methods=["POST"]) 
def admin_remove_employee(emp_id):
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor(); cur.execute("DELETE FROM employees WHERE id=%s" if USE_POSTGRES else "DELETE FROM employees WHERE id=?", (emp_id,))
    conn.commit(); conn.close()
    flash("Employee removed","info"); return redirect(url_for("admin_dashboard"))


@app.route("/admin/employee/<int:emp_id>/timecards", methods=["GET","POST"]) 
def admin_timecards(emp_id):
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    if request.method=="POST":
        date = request.form['date']; time_in = request.form['time_in']; time_out = request.form['time_out']
        cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out) VALUES (%s,%s,%s,%s)" if USE_POSTGRES else "INSERT INTO timecards (employee_id,date,time_in,time_out) VALUES (?,?,?,?)", (emp_id,date,time_in,time_out))
        conn.commit(); flash("Timecard added","success")
    cur.execute("SELECT * FROM employees WHERE id=%s" if USE_POSTGRES else "SELECT * FROM employees WHERE id=?", (emp_id,))
    emp = fetchone_dict(cur)
    cur.execute("SELECT * FROM timecards WHERE employee_id=%s ORDER BY date DESC" if USE_POSTGRES else "SELECT * FROM timecards WHERE employee_id=? ORDER BY date DESC", (emp_id,))
    tcs = fetchall_dict(cur); conn.close()
    return render_template("admin_timecards.html", emp=emp, timecards=tcs)


@app.route("/admin/payroll/generate/<int:emp_id>", methods=["GET","POST"]) 
def admin_generate_payroll(emp_id):
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=%s" if USE_POSTGRES else "SELECT * FROM employees WHERE id=?", (emp_id,))
    emp = fetchone_dict(cur)
    cur.execute("SELECT * FROM payrolls WHERE employee_id=%s ORDER BY created_at DESC LIMIT 1" if USE_POSTGRES else "SELECT * FROM payrolls WHERE employee_id=? ORDER BY created_at DESC LIMIT 1", (emp_id,))
    last = fetchone_dict(cur)
    last_inputs = {}
    if last:
        last_inputs = json.loads(last["data"]).get("inputs", {})
    if request.method=="POST":
        form = request.form.to_dict()
        start_date = form.get("start_date")
        end_date = form.get("end_date")
        if not start_date or not end_date:
            flash("Please provide start and end dates for the cut-off", "danger")
            conn.close()
            return redirect(request.url)
        tc_hours = compute_timecard_hours(emp, conn, start_date, end_date)
        inputs = {}
        inputs.update(tc_hours)
        for key in ["incentives","tardiness_hours","undertime_hours","sss_loan","pagibig_loan","special_hol_hours","special_hol_rd_hours","regular_hol_hours","regular_hol_ot_hours"]:
            if key in form and form.get(key,"").strip()!="":
                try:
                    inputs[key] = float(form.get(key))
                except:
                    inputs[key] = 0.0
        if form.get("daily_rate"): inputs["daily_rate"] = float(form.get("daily_rate"))
        if form.get("hourly_rate"): inputs["hourly_rate"] = float(form.get("hourly_rate"))
        # deduction checkboxes - only include them if explicitly checked
        inputs["apply_sss"] = True if form.get("apply_sss") else False
        inputs["apply_phil"] = True if form.get("apply_phil") else False
        inputs["apply_pagibig"] = True if form.get("apply_pagibig") else False
        inputs["cutoff_start"] = start_date
        inputs["cutoff_end"] = end_date

        result = compute_payroll(emp, inputs)
        # persist payroll
        if USE_POSTGRES:
            cur.execute("INSERT INTO payrolls (employee_id, data, created_at) VALUES (%s,%s,%s)", (emp_id, json.dumps(result), datetime.datetime.utcnow().isoformat()))
        else:
            cur.execute("INSERT INTO payrolls (employee_id, data, created_at) VALUES (?,?,?)", (emp_id, json.dumps(result), datetime.datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        flash("Payroll generated and saved","success")
        return redirect(url_for("admin_dashboard"))
    conn.close()
    return render_template("admin_generate_payroll.html", emp=emp, last_inputs=last_inputs)


@app.route("/employee/home")
def employee_home():
    if not session.get("employee_id"): return redirect(url_for("employee_login"))
    emp_id = session["employee_id"]
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=%s" if USE_POSTGRES else "SELECT * FROM employees WHERE id=?", (emp_id,))
    emp = fetchone_dict(cur)
    cur.execute("SELECT id, created_at FROM payrolls WHERE employee_id=%s ORDER BY created_at DESC" if USE_POSTGRES else "SELECT id, created_at FROM payrolls WHERE employee_id=? ORDER BY created_at DESC", (emp_id,))
    pays = fetchall_dict(cur); conn.close()
    return render_template("employee_home.html", emp=emp, pays=pays)


@app.route("/payroll/view/<int:pid>")
def payroll_view(pid):
    if not session.get("employee_id") and not session.get("is_admin"): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor(); cur.execute("SELECT * FROM payrolls WHERE id=%s" if USE_POSTGRES else "SELECT * FROM payrolls WHERE id=?", (pid,))
    row = fetchone_dict(cur)
    if not row: flash("Payslip not found","danger"); conn.close(); return redirect(url_for("index"))
    if not session.get("is_admin") and row["employee_id"] != session.get("employee_id"):
        flash("Unauthorized to view this payslip","danger"); conn.close(); return redirect(url_for("index"))
    data = json.loads(row["data"]); conn.close()
    return render_template("payroll_view.html", data=data, pid=pid)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=True)
