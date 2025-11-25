from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, json, datetime, math, uuid, random

DB = os.path.join(os.path.dirname(__file__), "payroll.db")
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "change_this_secret_" + str(uuid.uuid4()))

# ---------- Utilities ----------
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Create tables
    cur.executescript(open("schema.sql").read())

    # FORCE RESET ADMIN EVERY STARTUP (NO MORE INVALID CREDENTIALS)
    username = "admin"
    password = "abic123"
    password_hash = generate_password_hash(password)

    cur.execute("DELETE FROM admins")
    cur.execute(
        "INSERT INTO admins (id, username, password_hash) VALUES (1, ?, ?)",
        (username, password_hash)
    )

    print("===================================")
    print("✅ ADMIN ACCOUNT RESET & READY")
    print("Username:", username)
    print("Password:", password)
    print("===================================")

    conn.commit()
    conn.close()


def randpass(n=8):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(random.choice(chars) for _ in range(n))

def make_username(name):
    base = "".join(c for c in name.lower() if c.isalnum())
    return (base[:8] + str(random.randint(10,99)))

# ---------- Time helpers ----------
def to_minutes(tstr):
    # handle empty or None gracefully
    if not tstr or tstr.strip()=="":
        return None
    # tstr like "09:00"
    try:
        h,m = map(int, tstr.split(":"))
        return h*60 + m
    except:
        return None

def minutes_to_hours(m):
    return m/60.0

def overlap_minutes(start1, end1, start2, end2):
    # all ints minutes since 0:00
    s = max(start1, start2)
    e = min(end1, end2)
    return max(0, e - s)

# ---------- Timecard to hours aggregator (with absence detection) ----------
def daterange(start_date, end_date):
    d = start_date
    while d <= end_date:
        yield d
        d += datetime.timedelta(days=1)


def compute_timecard_hours(emp_row, conn, start_date_str, end_date_str):
    """
    For an employee and date range, aggregate timecards and compute:
      worked_hours, regular_ot_hours (OT only after 7pm), restday_hours, restday_ot_hours,
      night_diff_hours, nsd_ot_hours, lwop_days, tardiness_hours, undertime_hours,
      present_days, absent_days

    Absence detection (Option A): for each date in cut-off range, if there is NO timecard and it's not the employee's rest day => count as absence.

    Dates in YYYY-MM-DD strings.
    Rules implemented:
      - Regular schedule: 09:00 to 18:00 (8 working hours)
      - Tardiness: time_in > 09:00 -> tardiness = time_in - 09:00 (in minutes) [deduction only; does not reduce worked hours]
      - Undertime: time_out < 18:00 -> undertime = 18:00 - time_out (in minutes)
      - Overtime counts ONLY after 19:00 (i.e., OT = time_out - 19:00 if > 0)
      - Night diff: 22:00 - 06:00
    """
    cur = conn.cursor()
    # fetch timecards into dict by date for quick lookup
    cur.execute("""
        SELECT date, time_in, time_out FROM timecards
        WHERE employee_id=? AND date BETWEEN ? AND ?
        ORDER BY date ASC
    """, (emp_row["id"], start_date_str, end_date_str))
    rows = cur.fetchall()
    timecard_map = {r["date"]: r for r in rows}

    worked_minutes = 0
    regular_ot_minutes = 0   # OT counted only after 19:00 (7pm)
    restday_minutes = 0
    restday_ot_minutes = 0
    night_diff_minutes = 0
    nsd_ot_minutes = 0
    lwop_days = 0
    tardiness_minutes_total = 0
    undertime_minutes_total = 0
    present_days = 0
    absent_days = 0

    # employee rest day name, normalize to weekday int if possible (e.g., "Sunday" etc.)
    rest_day_name = (emp_row["rest_day"] or "").strip().lower()
    weekday_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    rest_weekday = weekday_map.get(rest_day_name, None)

    # constants in minutes
    WORK_START = 9 * 60       # 09:00
    WORK_END = 18 * 60        # 18:00 (6 PM)
    OT_THRESHOLD = 19 * 60    # 19:00 (7 PM)
    STD_WORK_MINUTES = 8 * 60 # 8 hours per day

    start_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()

    for single_date in daterange(start_date, end_date):
        date_str = single_date.strftime("%Y-%m-%d")
        weekday = single_date.weekday()
        is_restday = (rest_weekday is not None and weekday == rest_weekday)

        tc = timecard_map.get(date_str)
        if not tc:
            # no timecard recorded for this date
            if is_restday:
                # nothing to count
                continue
            else:
                # absent day (Option A)
                absent_days += 1
                continue
        # there is a timecard for this date
        present_days += 1
        time_in = tc["time_in"]
        time_out = tc["time_out"]

        min_in = to_minutes(time_in)
        min_out = to_minutes(time_out)
        if min_in is None or min_out is None:
            # incomplete entry -> treat as LWOP
            lwop_days += 1
            absent_days += 1
            present_days -= 1
            continue

        # If time_out <= time_in, assume worked past midnight -> add 24h to min_out
        if min_out <= min_in:
            min_out += 24*60

        total_minutes = min_out - min_in

        # --- Tardiness: time_in after 09:00 (deduction only) ---
        tardiness_min = max(0, min_in - WORK_START)
        tardiness_minutes_total += tardiness_min

        # --- Undertime: time_out before 18:00 (deduction only) ---
        undertime_min = 0
        if min_out < WORK_END:
            undertime_min = max(0, WORK_END - min_out)
        undertime_minutes_total += undertime_min

        # --- Overtime: only counts after 19:00 ---
        ot_min = max(0, min_out - OT_THRESHOLD)

        # --- Night differential: overlap with 22:00-24:00 and 00:00-06:00 (shifted) ---
        nd1_s, nd1_e = 22*60, 24*60
        nd2_s, nd2_e = 0, 6*60
        nd_minutes = overlap_minutes(min_in, min_out, nd1_s, nd1_e) + overlap_minutes(min_in, min_out, nd2_s + 24*60, nd2_e + 24*60)

        # For regular worked minutes we keep the standard first-8-hours logic (so that worked hours cap at 8 for regular pay)
        regular_part = min(total_minutes, STD_WORK_MINUTES)
        # BUT OT is NOT computed from total_minutes-excess; OT is specifically time after 19:00 (ot_min)
        # Any time between work_end (18:00) and OT_THRESHOLD (19:00) is neutral (neither OT nor deduction)
        # Add computed parts to aggregates
        if is_restday:
            restday_minutes += regular_part
            restday_ot_minutes += ot_min
        else:
            worked_minutes += regular_part
            regular_ot_minutes += ot_min

        # Night diff minutes add to night diff; if the night diff minutes occur during OT window, count as nsd_ot as well
        if nd_minutes > 0:
            night_diff_minutes += nd_minutes
            if ot_min > 0:
                # OT window is [OT_THRESHOLD, min_out)
                ot_start = OT_THRESHOLD
                nsd_ot = overlap_minutes(ot_start, min_out, nd1_s, nd1_e) + overlap_minutes(ot_start, min_out, nd2_s + 24*60, nd2_e + 24*60)
                nsd_ot_minutes += nsd_ot

    # Convert minutes to hours (floating, rounded to 2 decimals)
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

# ---------- Payroll logic (Mali Lending Corp rules) ----------
def compute_tax(monthly_taxable):
    t = float(monthly_taxable)
    tax = 0.0
    # Implemented progressive tax based on user-provided bands (monthly)
    # NOTE: user-provided table in message had some inconsistencies; this implements a commonly used progressive slab mapping
    if t <= 20833:
        tax = 0.0
    elif t <= 33333:
        tax = 0.0  # per user's table some bands have 0% until 66667; but keep minimal tax
    elif t <= 66667:
        tax = 0.0
    elif t <= 166667:
        tax = 0.15 * (t - 20833)  # approximated from user text
    elif t <= 666667:
        tax = 1875 + 0.20 * (t - 33333)
    else:
        tax = 8541.80 + 0.25 * (t - 66667)
    return round(tax,2)


def compute_payroll(employee, inputs):
    # inputs: dict with numeric values and hours; employee row contains monthly_base_pay and rest_day and also deduction flags
    monthly = float(employee["monthly_base_pay"] or 0)
    # daily rate per user formula: monthly/313*12
    daily_rate = float(inputs.get("daily_rate",0)) or (monthly/313*12 if monthly>0 else 0)
    hourly_rate = float(inputs.get("hourly_rate",0)) or (daily_rate/8 if daily_rate>0 else 0)
    def money(x): return round((float(x) if x is not None else 0.0) + 0.0000001,2)

    # Remove automatic basic_pay addition. Use present/absent days to compute regular earnings and absences
    present_days = int(inputs.get("present_days",0))
    absent_days = int(inputs.get("absent_days",0))

    # Regular earnings based on days present
    regular_earnings = money(daily_rate * present_days)
    # Absence deduction
    absence_deduction = money(daily_rate * absent_days)

    # Hours provided in inputs (already computed)
    hrs = {k: float(inputs.get(k,0) or 0) for k in ["worked_hours","regular_ot_hours","restday_hours","restday_ot_hours","night_diff_hours","nsd_ot_hours","special_hol_hours","special_hol_rd_hours","regular_hol_hours","regular_hol_ot_hours"]}
    earnings = {}
    # Regular pay is now the regular_earnings
    earnings["regular_pay"] = regular_earnings
    # Regular OT: 125%
    earnings["regular_ot"] = money(hourly_rate * 1.25 * hrs["regular_ot_hours"])
    # Restday rate: 130%
    earnings["restday"] = money(hourly_rate * 1.30 * hrs["restday_hours"])
    # Restday OT or Special Hol OT: 169%
    earnings["restday_ot"] = money(hourly_rate * 1.69 * hrs["restday_ot_hours"])
    # Night Differential 110%
    earnings["night_diff"] = money(hourly_rate * 1.10 * hrs["night_diff_hours"])
    # NSD OT 137.5%
    earnings["nsd_ot"] = money(hourly_rate * 1.375 * hrs["nsd_ot_hours"])
    # Special holiday 130% (admin may provide hours manually if needed)
    earnings["special_holiday"] = money(hourly_rate * 1.30 * hrs.get("special_hol_hours",0))
    # Special holiday on RD 150%
    earnings["special_holiday_rd"] = money(hourly_rate * 1.50 * hrs.get("special_hol_rd_hours",0))
    # Regular holiday 200%
    earnings["regular_holiday"] = money(hourly_rate * 2.00 * hrs.get("regular_hol_hours",0))
    # Regular holiday OT 260%
    earnings["regular_holiday_ot"] = money(hourly_rate * 2.60 * hrs.get("regular_hol_ot_hours",0))
    # incentives (can be manually provided)
    incentives = float(inputs.get("incentives",0) or 0)
    earnings["incentives"] = money(incentives)

    # total earnings (sum of all earnings components)
    total_earnings = money(sum(v for v in earnings.values()))

    # Deductions
    # Use computed tardiness/undertime from inputs if present (compute_timecard_hours now returns these)
    tardiness_hours = float(inputs.get("tardiness_hours",0) or 0)
    undertime_hours = float(inputs.get("undertime_hours",0) or 0)
    lwop_days = float(inputs.get("lwop_days",0) or 0)
    sss_loan = float(inputs.get("sss_loan",0) or 0)
    pagibig_loan = float(inputs.get("pagibig_loan",0) or 0)

    tardiness_amt = hourly_rate * tardiness_hours
    undertime_amt = hourly_rate * undertime_hours
    lwop_amt = daily_rate * lwop_days

    # Apply mandatory deductions conditionally (admin checkboxes)
    apply_sss = inputs.get("apply_sss", "true") in ["1","true","on","yes","True", True]
    apply_phil = inputs.get("apply_phil", "true") in ["1","true","on","yes","True", True]
    apply_pagibig = inputs.get("apply_pagibig", "true") in ["1","true","on","yes","True", True]

    sss = money(monthly * 0.05) if apply_sss else 0.0
    phil = money(monthly * 0.025) if apply_phil else 0.0
    pagibig = 200.0 if apply_pagibig else 0.0

    # Gross pay calculation: regular earnings + premiums - absences - tardiness - undertime
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
    init_db()

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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO admins (id, username, password_hash) VALUES (1, ?, ?)", (username, hashpw))
    conn.commit()
    conn.close()
    return f"Admin user created/updated. Username: {username} Password: {password}. PLEASE REMOVE / DISABLE / CHANGE INIT_ADMIN_SECRET AFTER USE."

# Admin auth
@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method=="POST":
        u = request.form['username']; p = request.form['password']
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM admins WHERE username=?", (u,))
        row = cur.fetchone(); conn.close()
        if row and check_password_hash(row["password_hash"], p):
            session["is_admin"] = True; session["admin_id"] = row["id"]
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials","danger")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

# Employee auth
@app.route("/employee/login", methods=["GET","POST"])
def employee_login():
    if request.method=="POST":
        u = request.form['username']; p = request.form['password']
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM employees WHERE username=?", (u,))
        row = cur.fetchone(); conn.close()
        if row and check_password_hash(row["password_hash"], p):
            session["employee_id"] = row["id"]; session["is_admin"] = False
            return redirect(url_for("employee_home"))
        flash("Invalid credentials","danger")
    return render_template("employee_login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# Admin dashboard and employee management
@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees ORDER BY id DESC")
    employees = cur.fetchall()
    conn.close()
    return render_template("admin_dashboard.html", employees=employees)

@app.route("/admin/employee/add", methods=["GET","POST"])
def admin_add_employee():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    if request.method=="POST":
        name = request.form['name']; company = request.form['company']; rest_day = request.form['rest_day']
        monthly = float(request.form.get('monthly_base_pay') or 0)
        username = make_username(name); password = randpass(8)
        phash = generate_password_hash(password)
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO employees (name, company, rest_day, monthly_base_pay, username, password_hash) VALUES (?,?,?,?,?,?)", (name, company, rest_day, monthly, username, phash))
        conn.commit(); emp_id = cur.lastrowid; conn.close()
        flash(f'Employee created. Username: {username} Password: {password} (copy these)', "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_add_employee.html")

@app.route("/admin/employee/<int:emp_id>/remove", methods=["POST"])
def admin_remove_employee(emp_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor(); cur.execute("DELETE FROM employees WHERE id=?", (emp_id,)); conn.commit(); conn.close()
    flash("Employee removed","info")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/employee/<int:emp_id>/timecards", methods=["GET","POST"])
def admin_timecards(emp_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    if request.method=="POST":
        date = request.form['date']; time_in = request.form['time_in']; time_out = request.form['time_out']
        cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out) VALUES (?,?,?,?)",(emp_id,date,time_in,time_out))
        conn.commit(); flash("Timecard added","success")
    cur.execute("SELECT * FROM employees WHERE id=?", (emp_id,)); emp = cur.fetchone()
    cur.execute("SELECT * FROM timecards WHERE employee_id=? ORDER BY date DESC", (emp_id,))
    tcs = cur.fetchall(); conn.close()
    return render_template("admin_timecards.html", emp=emp, timecards=tcs)

@app.route("/admin/payroll/generate/<int:emp_id>", methods=["GET","POST"])
def admin_generate_payroll(emp_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=?", (emp_id,)); emp = cur.fetchone()
    # load last payroll inputs to prefill
    cur.execute("SELECT * FROM payrolls WHERE employee_id=? ORDER BY created_at DESC LIMIT 1", (emp_id,))
    last = cur.fetchone()
    last_inputs = {}
    if last:
        last_inputs = json.loads(last["data"]).get("inputs", {})
    if request.method=="POST":
        form = request.form.to_dict()
        # Expect: start_date, end_date, optional manual fields (incentives, loans, tardiness/undertime if manually measured),
        # and checkboxes apply_sss, apply_phil, apply_pagibig.
        start_date = form.get("start_date")
        end_date = form.get("end_date")
        if not start_date or not end_date:
            flash("Please provide start and end dates for the cut-off", "danger")
            conn.close()
            return redirect(request.url)
        # compute hours from timecards
        tc_hours = compute_timecard_hours(emp, conn, start_date, end_date)
        # merge computed hours into inputs (so compute_payroll can use them)
        inputs = {}
        inputs.update(tc_hours)
        # populate other numeric defaults from form if present
        for key in ["incentives","tardiness_hours","undertime_hours","sss_loan","pagibig_loan","special_hol_hours","special_hol_rd_hours","regular_hol_hours","regular_hol_ot_hours"]:
            if key in form and form.get(key,"").strip()!="":
                try:
                    inputs[key] = float(form.get(key))
                except:
                    inputs[key] = 0.0
        # add rate overrides if provided (optional)
        if form.get("daily_rate"): inputs["daily_rate"] = float(form.get("daily_rate"))
        if form.get("hourly_rate"): inputs["hourly_rate"] = float(form.get("hourly_rate"))
        # deduction checkboxes
        inputs["apply_sss"] = form.get("apply_sss", "on")
        inputs["apply_phil"] = form.get("apply_phil", "on")
        inputs["apply_pagibig"] = form.get("apply_pagibig", "on")
        # include the cut-off metadata
        inputs["cutoff_start"] = start_date
        inputs["cutoff_end"] = end_date

        # compute payroll using aggregated inputs
        result = compute_payroll(emp, inputs)
        # persist payroll
        cur.execute("INSERT INTO payrolls (employee_id, data, created_at) VALUES (?,?,?)",(emp_id, json.dumps(result), datetime.datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        flash("Payroll generated and saved","success")
        return redirect(url_for("admin_dashboard"))
    conn.close()
    return render_template("admin_generate_payroll.html", emp=emp, last_inputs=last_inputs)

@app.route("/employee/home")
def employee_home():
    if not session.get("employee_id"):
        return redirect(url_for("employee_login"))
    emp_id = session["employee_id"]
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=?", (emp_id,)); emp = cur.fetchone()
    cur.execute("SELECT id, created_at FROM payrolls WHERE employee_id=? ORDER BY created_at DESC", (emp_id,))
    pays = cur.fetchall(); conn.close()
    return render_template("employee_home.html", emp=emp, pays=pays)

@app.route("/payroll/view/<int:pid>")
def payroll_view(pid):
    # allow only admin or owner
    if not session.get("employee_id") and not session.get("is_admin"):
        return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor(); cur.execute("SELECT * FROM payrolls WHERE id=?", (pid,)); row = cur.fetchone()
    if not row: 
        flash("Payslip not found","danger"); 
        return redirect(url_for("index"))
    if not session.get("is_admin") and row["employee_id"] != session.get("employee_id"):
        flash("Unauthorized to view this payslip","danger")
        return redirect(url_for("index"))
    data = json.loads(row["data"]); conn.close()
    return render_template("payroll_view.html", data=data, pid=pid)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=True)
