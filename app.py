
from flask import Flask, render_template, request, redirect, session
import sqlite3

app = Flask(__name__)
app.secret_key = "abic_secret_key"

def get_db():
    return sqlite3.connect("payroll.db")

@app.route("/")
def login():
    return render_template("login.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")

if __name__ == "__main__":
    app.run(debug=True)
