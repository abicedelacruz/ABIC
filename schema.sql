
-- Schema for full payroll system
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password_hash TEXT
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    company TEXT,
    rest_day TEXT,
    monthly_base_pay REAL DEFAULT 0,
    username TEXT UNIQUE,
    password_hash TEXT
);

CREATE TABLE IF NOT EXISTS timecards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER,
    date TEXT,
    time_in TEXT,
    time_out TEXT
);

CREATE TABLE IF NOT EXISTS payrolls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER,
    data TEXT,
    created_at TEXT
);

-- Seed admin (username: admin, password: abic123)
INSERT OR IGNORE INTO admins (id, username, password_hash) VALUES (1, 'admin', 'pbkdf2:sha256:260000$uUCv5n0T$5a0b6e8b5c9f0fd2d5c2dfb71b77c8b12ce9a0d6a6b77af4cfdadbc6c43a4030');
