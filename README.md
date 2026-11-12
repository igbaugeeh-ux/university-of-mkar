# University of Mkar Examination Scheduling System

A Flask application for entering examination data, generating clash-free timetables, allocating invigilators, and exporting the result. SQLite is the default database; MySQL is supported through MySQL Connector/Python. The separate command-line scheduler remains available as `scheduler.py`.

## Requirements

Use Python 3.12 with the project virtual environment. The checked-in VS Code setting selects `.venv312` when the project folder is opened. Create it and install the dependencies with:

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>. The application binds to localhost by default. The database is created at `instance/examinations.sqlite3`. On a fresh database, use **Manage records** to enter the source data in the web interface. Existing database records are preserved when the app starts.

## MySQL Connection

Create the database in MySQL before starting the app:

```sql
CREATE DATABASE mkar_examinations CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Set the connection values in PowerShell, then start Flask from that same terminal. Do not commit real credentials to the project:

```powershell
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "your_mysql_user"
$env:MYSQL_PASSWORD = "your_mysql_password"
$env:MYSQL_DATABASE = "mkar_examinations"
python app.py
```

When `MYSQL_DATABASE` is set, the app uses `mysql+mysqlconnector` and creates its tables on startup. Alternatively, provide a SQLAlchemy `DATABASE_URL`. The database connection check is available at <http://127.0.0.1:5000/health/database>; it returns HTTP 200 when connected and HTTP 503 when the configured server cannot be reached. Without MySQL settings, the app continues to use SQLite.

## Security

The app enables CSRF protection, HttpOnly and SameSite session cookies, and baseline browser security headers. The Flask development server is intended for local use only; do not expose it directly to the internet. Production mode refuses to start unless `SECRET_KEY` is at least 32 characters and `SESSION_COOKIE_SECURE=true`.

For deployment, terminate HTTPS at a trusted reverse proxy and keep the WSGI server bound to localhost. Generate a strong secret once and store it securely; reuse the same value across restarts. From an activated project environment, start the app with:

```powershell
$env:APP_ENV = "production"
$secret = Read-Host "Persistent secret key" -AsSecureString
$env:SECRET_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
$env:SESSION_COOKIE_SECURE = "true"
waitress-serve --host=127.0.0.1 --port=5000 wsgi:app
```

Keep database, mail, and secret-key credentials out of source control.

Run workflow tests with:

```powershell
python -m unittest discover -s tests -v
```

## Record Management

Choose **Manage records** on the dashboard to enter, view, edit, and delete students, courses, registrations, time slots, venues, invigilators, and availability. Add students and courses before their registrations, and add invigilators and slots before availability records. Record IDs remain fixed for single-key records; the composite keys for registrations and availability can be edited. Deletion is blocked while another record depends on the selected row. Any successful source-data change clears the generated timetable so it can be optimized again. The Flask app does not load CSV or workbook input; the standalone `scheduler.py` command-line prototype still reads CSV files when run directly.

## Examiner Accounts

Examiners register with their name, department, institutional email, and password at `/register`. New accounts remain pending and cannot sign in until approved. Administrators review applications at `/admin/examiners`, where they can approve or reject them. Passwords are stored as hashes, and changes to examiner accounts are protected by CSRF tokens.

Password recovery is available from the login page. Configure SMTP in the same environment as the app (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, and optionally `SMTP_USE_TLS`) to deliver one-hour, single-use reset links. Reset requests use a generic response whether the address exists or not. Set a stable `SECRET_KEY` environment variable so outstanding reset links remain valid across app restarts.

Example PowerShell configuration (use your provider's SMTP host and an app-specific mail password; do not commit credentials):

```powershell
$env:SMTP_HOST = "smtp.example.edu"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "exams@example.edu"
$env:SMTP_FROM = "exams@example.edu"
$env:SMTP_USE_TLS = "true"
$env:SMTP_PASSWORD = Read-Host "SMTP app password"
$env:SECRET_KEY = Read-Host "Long random app secret"
```

### First Administrator

When no administrator exists, the login page links to **Create the first administrator** at `/setup-admin`. Enter a name, email, and password of at least 12 characters. This one-time local setup page closes after the first administrator is created. Existing examiner accounts are marked approved during the schema migration, but none become administrators automatically. All approved examiners currently share access to the same records; department-specific permissions are not yet implemented.

If an existing administrator cannot sign in, run `python setup_admin.py` to create or reset an administrator account. Run it from the same PowerShell environment and project folder used to start the app, so it uses the same `DATABASE_URL` or `MYSQL_*` settings. With no database settings, both the app and the script use the local SQLite database. The script prompts for the account email and a new password; use those exact credentials at `/login`.

## Optimization Model

The CP-SAT model assigns each course exactly once to a sufficiently long slot and a venue with enough capacity. It prevents overlapping examinations for any student sharing both courses, prevents a venue from holding simultaneous exams, and assigns the configured number of invigilators per exam. Invigilator assignments respect course qualifications, slot availability, overlapping duties, and maximum duty counts.

The objective minimizes the number of distinct slots first, then prefers rooms with less unused capacity. Solver status and runtime are saved with each successful run and displayed on the dashboard. Export the timetable and invigilator roster using **Export .xlsx**.

## Evaluation

The dashboard records courses scheduled, conflict pairs resolved, slots used, duties assigned, solver status, and runtime. For the project evaluation, record a manual-scheduling baseline using the same course registrations, slot options, room capacities, and staffing requirements. Compare preparation time, remaining student clashes, number of slots and rooms used, and invigilator workload distribution. Solver runtime alone is not a fair comparison with manual preparation time; record the human effort separately.

## Local Prototype Scope

The dashboard has examiner login and CSRF protection, but it is intended for a trusted local machine. Before deployment on an institutional network, add account approval or institutional identity verification, department-based authorization if required, password reset, HTTPS, backups, and operational security controls.
