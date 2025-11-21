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

    # Create tables from schema
    cur.executescript(open("schema.sql").read())

    # FORCE create admin account if missing
    cur.execute("SELECT * FROM admins WHERE username = ?", ("admin",))
    if not cur.fetchone():
        admin_password = "admin123"
        admin_hash = generate_password_hash(admin_password)

        cur.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?,?)",
            ("admin", admin_hash)
        )

        print("===================================")
        print("✅ ADMIN ACCOUNT CREATED")
        print("Username: admin")
        print("Password: admin123")
        print("===================================")

    conn.commit()
    conn.close()


def randpass(n=8):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(random.choice(chars) for _ in range(n))

def make_username(name):
    base = "".join(c for c in name.lower() if c.isalnum())
    return (base[:8] + str(random.randint(10,99)))

# ---------- Time parsing helpers ----------
def to_minutes(tstr):
    # tstr like "09:00"
    h,m = map(int, tstr.split(":"))
    return h*60 + m

def minutes_to_hours(m):
    return m/60.0

# ---------- Payroll logic (Mali Lending Corp rules) ----------
def compute_tax(monthly_taxable):
    t = float(monthly_taxable)
    tax = 0.0
    # Implemented progressive tax based on user-provided bands (monthly)
    if t <= 20833:
        tax = 0.0
    elif t <= 33333:
        tax = 0.15 * (t - 20833)
    elif t <= 66667:
        tax = 1875 + 0.20 * (t - 33333)
    elif t <= 166667:
        tax = 33541.80 + 0.30 * (t - 66667)
    elif t <= 666667:
        tax = 183541.80 + 0.35 * (t - 166667)
    else:
        tax = 83541.80 + 0.25 * (t - 666667)
    return round(tax,2)

def compute_payroll(employee, inputs):
    # inputs: dict with numeric values and hours; employee row contains monthly_base_pay and rest_day
    monthly = float(employee["monthly_base_pay"] or 0)
    # daily rate per user formula: monthly/313*12  (we'll implement daily = monthly / 26? but using user's formula)
    try:
        daily_rate = float(inputs.get("daily_rate",0)) or (monthly/313*12 if monthly>0 else 0)
    except:
        daily_rate = 0.0
    hourly_rate = float(inputs.get("hourly_rate",0)) or (daily_rate/8 if daily_rate>0 else 0)
    def money(x): return round(x+0.0000001,2)

    # base pay for the cut-off: assume half-month paid
    basic_pay = monthly/2.0

    # compute earnings from various hour inputs
    hrs = {k: float(inputs.get(k,0) or 0) for k in ["worked_hours","regular_ot_hours","restday_hours","restday_ot_hours","night_diff_hours","nsd_ot_hours","special_hol_hours","special_hol_rd_hours","regular_hol_hours","regular_hol_ot_hours"]}
    earnings = {}
    earnings["basic_pay"] = money(basic_pay)
    # Regular pay: hourly * worked_hours
    earnings["regular_pay"] = money(hourly_rate * hrs["worked_hours"])
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
    # Special holiday 130%
    earnings["special_holiday"] = money(hourly_rate * 1.30 * hrs["special_hol_hours"])
    # Special holiday on RD 150%
    earnings["special_holiday_rd"] = money(hourly_rate * 1.50 * hrs["special_hol_rd_hours"])
    # Regular holiday 200%
    earnings["regular_holiday"] = money(hourly_rate * 2.00 * hrs["regular_hol_hours"])
    # Regular holiday OT 260%
    earnings["regular_holiday_ot"] = money(hourly_rate * 2.60 * hrs["regular_hol_ot_hours"])
    # incentives
    incentives = float(inputs.get("incentives",0) or 0)
    earnings["incentives"] = money(incentives)

    total_earnings = sum(earnings.values())

    # Deductions
    tardiness_hours = float(inputs.get("tardiness_hours",0) or 0)
    undertime_hours = float(inputs.get("undertime_hours",0) or 0)
    lwop_days = float(inputs.get("lwop_days",0) or 0)
    sss_loan = float(inputs.get("sss_loan",0) or 0)
    pagibig_loan = float(inputs.get("pagibig_loan",0) or 0)

    tardiness_amt = hourly_rate * tardiness_hours
    undertime_amt = hourly_rate * undertime_hours
    lwop_amt = daily_rate * lwop_days
    sss = money(monthly * 0.05)
    phil = money(monthly * 0.025)
    pagibig = 200.0

    subtotal = total_earnings + basic_pay
    taxable = subtotal - (sss + phil + pagibig)
    tax = compute_tax(taxable)

    total_deductions = money(tardiness_amt + undertime_amt + lwop_amt + sss + phil + pagibig + sss_loan + pagibig_loan + tax)

    net = money((basic_pay + total_earnings) - total_deductions)

    result = {
        "rates": {"monthly": money(monthly), "daily_rate": money(daily_rate), "hourly_rate": money(hourly_rate)},
        "earnings": earnings,
        "total_earnings": money(total_earnings + basic_pay),
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

# --- ADMIN INITIALIZER (one-time use) ---
# Usage: GET /init_admin?secret=YOUR_SECRET
# Set environment variable INIT_ADMIN_SECRET to a strong secret on production.
# After running once, remove or disable this route for security.
@app.route("/init_admin")
def init_admin():
    # read secret from env
    expected = os.environ.get("INIT_ADMIN_SECRET", "init_admin_secret")
    provided = request.args.get("secret", "")
    if provided != expected:
        return ("Forbidden: invalid secret. Set INIT_ADMIN_SECRET or provide correct secret in query string.", 403)
    # create or update admin account
    username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
    password = os.environ.get("INITIAL_ADMIN_PASSWORD", "abic123")
    hashpw = generate_password_hash(password)
    conn = get_db()
    cur = conn.cursor()
    # use INSERT OR REPLACE to ensure the admin row exists and is updated
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
    session.clear(); return redirect(url_for("index"))

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
    session.clear(); return redirect(url_for("index"))

# Admin dashboard and employee management
@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees ORDER BY id DESC")
    employees = cur.fetchall()
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
        cur.execute("INSERT INTO employees (name, company, rest_day, monthly_base_pay, username, password_hash) VALUES (?,?,?,?,?,?)",
                    (name, company, rest_day, monthly, username, phash))
        conn.commit(); emp_id = cur.lastrowid; conn.close()
        flash(f'Employee created. Username: {username} Password: {password} (copy these)', "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_add_employee.html")

@app.route("/admin/employee/<int:emp_id>/remove", methods=["POST"])
def admin_remove_employee(emp_id):
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor(); cur.execute("DELETE FROM employees WHERE id=?", (emp_id,)); conn.commit(); conn.close()
    flash("Employee removed","info"); return redirect(url_for("admin_dashboard"))

# Timecards
@app.route("/admin/employee/<int:emp_id>/timecards", methods=["GET","POST"])
def admin_timecards(emp_id):
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    if request.method=="POST":
        date = request.form['date']; time_in = request.form['time_in']; time_out = request.form['time_out']
        cur.execute("INSERT INTO timecards (employee_id,date,time_in,time_out) VALUES (?,?,?,?)",(emp_id,date,time_in,time_out))
        conn.commit(); flash("Timecard added","success")
    cur.execute("SELECT * FROM employees WHERE id=?", (emp_id,)); emp = cur.fetchone()
    cur.execute("SELECT * FROM timecards WHERE employee_id=? ORDER BY date DESC", (emp_id,))
    tcs = cur.fetchall(); conn.close()
    return render_template("admin_timecards.html", emp=emp, timecards=tcs)

# Generate payroll (prefill with last payroll if exists)
@app.route("/admin/payroll/generate/<int:emp_id>", methods=["GET","POST"])
def admin_generate_payroll(emp_id):
    if not session.get("is_admin"): return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=?", (emp_id,)); emp = cur.fetchone()
    # load last payroll inputs to prefill
    cur.execute("SELECT * FROM payrolls WHERE employee_id=? ORDER BY created_at DESC LIMIT 1", (emp_id,))
    last = cur.fetchone()
    last_inputs = {}
    if last:
        last_inputs = json.loads(last["data"]).get("inputs", {})
    if request.method=="POST":
        inputs = request.form.to_dict()
        # convert numeric fields robustly
        for k,v in inputs.items():
            # keep strings for dates; else ensure numeric-like where appropriate
            pass
        result = compute_payroll(emp, inputs)
        cur.execute("INSERT INTO payrolls (employee_id, data, created_at) VALUES (?,?,?)",(emp_id, json.dumps(result), datetime.datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        flash("Payroll generated and saved","success"); return redirect(url_for("admin_dashboard"))
    conn.close()
    return render_template("admin_generate_payroll.html", emp=emp, last_inputs=last_inputs)

# Employee home & payslips
@app.route("/employee/home")
def employee_home():
    if not session.get("employee_id"): return redirect(url_for("employee_login"))
    emp_id = session["employee_id"]
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=?", (emp_id,)); emp = cur.fetchone()
    cur.execute("SELECT id, created_at FROM payrolls WHERE employee_id=? ORDER BY created_at DESC", (emp_id,))
    pays = cur.fetchall(); conn.close()
    return render_template("employee_home.html", emp=emp, pays=pays)

@app.route("/payroll/view/<int:pid>")
def payroll_view(pid):
    # allow only admin or owner
    if not session.get("employee_id") and not session.get("is_admin"): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor(); cur.execute("SELECT * FROM payrolls WHERE id=?", (pid,)); row = cur.fetchone()
    if not row: flash("Payslip not found","danger"); return redirect(url_for("index"))
    if not session.get("is_admin") and row["employee_id"] != session.get("employee_id"):
        flash("Unauthorized to view this payslip","danger"); return redirect(url_for("index"))
    data = json.loads(row["data"]); conn.close()
    return render_template("payroll_view.html", data=data, pid=pid)

# Static files served automatically

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=True)
