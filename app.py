from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import smtplib
import ssl
import time
from collections import defaultdict
from datetime import datetime
from email.message import EmailMessage
from io import BytesIO
from itertools import combinations
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from itsdangerous import BadSignature, URLSafeTimedSerializer
import mysql.connector
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import URL, UniqueConstraint, inspect, make_url, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
load_dotenv()


class Student(db.Model):
    student_id = db.Column(db.String(80), primary_key=True)
    matric_number = db.Column(db.String(100), nullable=False)
    programme = db.Column(db.String(150), nullable=False)
    level = db.Column(db.String(40), nullable=False)


class Course(db.Model):
    course_id = db.Column(db.String(80), primary_key=True)
    course_code = db.Column(db.String(80), nullable=False)
    course_title = db.Column(db.String(200), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False)


class Registration(db.Model):
    student_id = db.Column(db.String(80), db.ForeignKey("student.student_id"), primary_key=True)
    course_id = db.Column(db.String(80), db.ForeignKey("course.course_id"), primary_key=True)


class ExamSlot(db.Model):
    slot_id = db.Column(db.String(80), primary_key=True)
    date = db.Column(db.String(10), nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)


class Venue(db.Model):
    venue_id = db.Column(db.String(80), primary_key=True)
    venue_name = db.Column(db.String(150), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)


class Invigilator(db.Model):
    invigilator_id = db.Column(db.String(80), primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    department = db.Column(db.String(150), nullable=False, default="")
    max_duties = db.Column(db.Integer)
    qualified_courses = db.Column(db.Text, nullable=False, default="*")


class Availability(db.Model):
    invigilator_id = db.Column(db.String(80), db.ForeignKey("invigilator.invigilator_id"), primary_key=True)
    slot_id = db.Column(db.String(80), db.ForeignKey("exam_slot.slot_id"), primary_key=True)
    available = db.Column(db.Boolean, nullable=False)


class ScheduledExam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.String(80), db.ForeignKey("course.course_id"), nullable=False, unique=True)
    slot_id = db.Column(db.String(80), db.ForeignKey("exam_slot.slot_id"), nullable=False)
    venue_id = db.Column(db.String(80), db.ForeignKey("venue.venue_id"), nullable=False)
    student_count = db.Column(db.Integer, nullable=False)

    course = db.relationship(Course)
    slot = db.relationship(ExamSlot)
    venue = db.relationship(Venue)
    duties = db.relationship("InvigilatorDuty", cascade="all, delete-orphan", order_by="InvigilatorDuty.invigilator_id")


class InvigilatorDuty(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("scheduled_exam.id"), nullable=False)
    invigilator_id = db.Column(db.String(80), db.ForeignKey("invigilator.invigilator_id"), nullable=False)
    invigilator = db.relationship(Invigilator)
    __table_args__ = (UniqueConstraint("exam_id", "invigilator_id"),)


class OptimizationRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    solver_status = db.Column(db.String(20), nullable=False)
    runtime_seconds = db.Column(db.Float, nullable=False)
    courses_scheduled = db.Column(db.Integer, nullable=False)
    conflict_pairs = db.Column(db.Integer, nullable=False)
    slots_used = db.Column(db.Integer, nullable=False)
    duties_assigned = db.Column(db.Integer, nullable=False)


class ExaminerAccount(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    department = db.Column(db.String(150), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    is_approved = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    password_reset_nonce = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


@login_manager.user_loader
def load_examiner(user_id):
    try:
        account_id = int(user_id)
    except (TypeError, ValueError):
        return None
    account = db.session.get(ExaminerAccount, account_id)
    return account if account is not None and account.is_approved else None


TABLE_FIELDS = {
    "students": ("student_id", "matric_number", "programme", "level"),
    "courses": ("course_id", "course_code", "course_title", "duration_minutes"),
    "registrations": ("student_id", "course_id"),
    "slots": ("slot_id", "date", "start_time", "end_time"),
    "venues": ("venue_id", "venue_name", "capacity"),
    "invigilators": ("invigilator_id", "name", "department", "max_duties", "qualified_courses"),
    "availability": ("invigilator_id", "slot_id", "available"),
}

RECORD_TABLES = {
    "students": (Student, "Students", "Student identities and programmes", ("student_id",), False),
    "courses": (Course, "Courses", "Course catalogue and exam durations", ("course_id",), False),
    "registrations": (Registration, "Registrations", "Student-course enrolments", ("student_id", "course_id"), True),
    "slots": (ExamSlot, "Time slots", "Available examination sessions", ("slot_id",), False),
    "venues": (Venue, "Venues", "Examination rooms and capacity", ("venue_id",), False),
    "invigilators": (Invigilator, "Invigilators", "Staff qualifications and duty limits", ("invigilator_id",), False),
    "availability": (Availability, "Availability", "Invigilator availability by slot", ("invigilator_id", "slot_id"), True),
}

RECORD_FIELD_TYPES = {
    "duration_minutes": ("number", 1), "capacity": ("number", 1),
    "max_duties": ("number", 0), "date": ("date", None),
    "start_time": ("time", None), "end_time": ("time", None),
    "available": ("boolean", None),
}


def _record_key(record, key_fields):
    values = [getattr(record, field) for field in key_fields]
    encoded = base64.urlsafe_b64encode(json.dumps(values).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _parse_record_key(record_key, key_fields):
    from flask import abort
    try:
        padded_key = record_key + "=" * (-len(record_key) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded_key).decode("utf-8"))
        if not isinstance(values, list) or len(values) != len(key_fields):
            raise ValueError
        return tuple(values) if len(values) > 1 else values[0]
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        abort(404)


def _record_form_fields(table_name):
    model, label, description, key_fields, allow_key_edit = RECORD_TABLES[table_name]
    fields = []
    for name in TABLE_FIELDS[table_name]:
        field_type, minimum = RECORD_FIELD_TYPES.get(name, ("text", None))
        fields.append({
            "name": name,
            "label": name.replace("_", " ").title().replace(" Id", " ID"),
            "type": field_type,
            "required": name != "max_duties",
            "minimum": minimum,
            "primary_key": name in key_fields,
        })
    return {
        "model": model, "label": label, "description": description,
        "keys": key_fields, "allow_key_edit": allow_key_edit, "fields": fields,
    }


def _parse_record_values(table_name, form):
    config = _record_form_fields(table_name)
    values = {}
    for field in config["fields"]:
        name = field["name"]
        raw_value = form.get(name, "").strip()
        if field["type"] == "boolean":
            values[name] = raw_value.casefold() in {"true", "1", "yes"}
        elif field["type"] == "number":
            if not raw_value and not field["required"]:
                values[name] = None
                continue
            try:
                number = int(raw_value)
            except ValueError:
                raise ValueError(f"{field['label']} must be a whole number.") from None
            if field["minimum"] is not None and number < field["minimum"]:
                raise ValueError(f"{field['label']} must be at least {field['minimum']}.")
            values[name] = number
        else:
            if field["required"] and not raw_value:
                raise ValueError(f"{field['label']} is required.")
            values[name] = raw_value or None

    if table_name == "registrations":
        if db.session.get(Student, values["student_id"]) is None:
            raise ValueError("Choose an existing student ID.")
        if db.session.get(Course, values["course_id"]) is None:
            raise ValueError("Choose an existing course ID.")
    elif table_name == "availability":
        if db.session.get(Invigilator, values["invigilator_id"]) is None:
            raise ValueError("Choose an existing invigilator ID.")
        if db.session.get(ExamSlot, values["slot_id"]) is None:
            raise ValueError("Choose an existing slot ID.")
    elif table_name == "slots":
        try:
            datetime.strptime(values["date"], "%Y-%m-%d")
            start = datetime.strptime(values["start_time"], "%H:%M")
            end = datetime.strptime(values["end_time"], "%H:%M")
            if start >= end:
                raise ValueError
        except ValueError:
            raise ValueError("Enter a valid date and an end time after the start time.") from None
    elif table_name == "invigilators":
        qualified = values["qualified_courses"] or "*"
        if qualified.strip() != "*":
            course_ids = {item.strip() for item in qualified.split(";") if item.strip()}
            known_courses = {course.course_id for course in Course.query.all()}
            if not course_ids <= known_courses:
                raise ValueError("Qualified courses must contain existing course IDs or *.")
            values["qualified_courses"] = ";".join(sorted(course_ids))
        else:
            values["qualified_courses"] = "*"
    return values


def _invalidate_timetable():
    db.session.query(InvigilatorDuty).delete()
    db.session.query(ScheduledExam).delete()


def _slot_overlaps(slot_a, slot_b):
    return (slot_a.date == slot_b.date and
            slot_a.start_time < slot_b.end_time and
            slot_b.start_time < slot_a.end_time)


def optimize_schedule(required_per_exam=2, time_limit_seconds=20):
    try:
        from ortools.sat.python import cp_model
    except ImportError as error:
        raise RuntimeError(
            "OR-Tools is not installed for this Python version. Use Python 3.14 or earlier, "
            "then install the packages in requirements.txt."
        ) from error

    courses = Course.query.order_by(Course.course_id).all()
    slots = ExamSlot.query.order_by(ExamSlot.date, ExamSlot.start_time).all()
    venues = Venue.query.order_by(Venue.capacity, Venue.venue_id).all()
    invigilators = Invigilator.query.order_by(Invigilator.invigilator_id).all()
    registrations = Registration.query.all()
    if not courses:
        raise ValueError("Import course and registration data before optimizing.")

    slot_by_id = {slot.slot_id: slot for slot in slots}
    student_counts = defaultdict(int)
    student_courses = defaultdict(set)
    for registration in registrations:
        student_courses[registration.student_id].add(registration.course_id)
        student_counts[registration.course_id] += 1
    conflict_pairs = set()
    for enrolled in student_courses.values():
        ordered = sorted(enrolled)
        conflict_pairs.update(combinations(ordered, 2))

    availability = {
        (row.invigilator_id, row.slot_id): row.available
        for row in Availability.query.all()
    }
    model = cp_model.CpModel()
    assignments = {}
    by_course = defaultdict(list)
    by_course_slot = defaultdict(list)
    by_slot = defaultdict(list)
    by_venue_slot = defaultdict(list)
    staffing = {}
    staff_by_invigilator_slot = defaultdict(list)
    staff_by_invigilator = defaultdict(list)

    for course in courses:
        duration_ok = []
        for slot in slots:
            start = datetime.strptime(slot.start_time, "%H:%M")
            end = datetime.strptime(slot.end_time, "%H:%M")
            if (end - start).total_seconds() / 60 >= course.duration_minutes:
                duration_ok.append(slot)
        eligible_venues = [venue for venue in venues if venue.capacity >= student_counts[course.course_id]]
        for slot in duration_ok:
            for venue in eligible_venues:
                key = (course.course_id, slot.slot_id, venue.venue_id)
                variable = model.NewBoolVar(f"exam_{course.course_id}_{slot.slot_id}_{venue.venue_id}")
                assignments[key] = variable
                by_course[course.course_id].append(variable)
                by_course_slot[(course.course_id, slot.slot_id)].append(variable)
                by_slot[slot.slot_id].append(variable)
                by_venue_slot[(venue.venue_id, slot.slot_id)].append(variable)
                eligible_invigilators = []
                for invigilator in invigilators:
                    qualified_raw = (invigilator.qualified_courses or "*").strip()
                    qualified = qualified_raw == "*" or course.course_id in {
                        item.strip() for item in qualified_raw.split(";") if item.strip()
                    }
                    if not qualified or not availability.get((invigilator.invigilator_id, slot.slot_id), True):
                        continue
                    staff_key = (*key, invigilator.invigilator_id)
                    staff_var = model.NewBoolVar(
                        f"staff_{course.course_id}_{slot.slot_id}_{venue.venue_id}_{invigilator.invigilator_id}"
                    )
                    staffing[staff_key] = staff_var
                    eligible_invigilators.append(staff_var)
                    staff_by_invigilator_slot[(invigilator.invigilator_id, slot.slot_id)].append(staff_var)
                    staff_by_invigilator[invigilator.invigilator_id].append(staff_var)
                model.Add(sum(eligible_invigilators) == required_per_exam * variable)
        if not by_course[course.course_id]:
            raise ValueError(
                f"Course {course.course_code} has no slot long enough and venue large enough for its enrollment."
            )
        model.Add(sum(by_course[course.course_id]) == 1)

    for course_a, course_b in conflict_pairs:
        for slot_a in slots:
            vars_a = by_course_slot.get((course_a, slot_a.slot_id), [])
            if not vars_a:
                continue
            for slot_b in slots:
                vars_b = by_course_slot.get((course_b, slot_b.slot_id), [])
                if vars_b and _slot_overlaps(slot_a, slot_b):
                    model.Add(sum(vars_a) + sum(vars_b) <= 1)

    for venue_id in {venue.venue_id for venue in venues}:
        venue_slots = [slot for slot in slots if by_venue_slot.get((venue_id, slot.slot_id))]
        for index, slot_a in enumerate(venue_slots):
            vars_a = by_venue_slot[(venue_id, slot_a.slot_id)]
            for slot_b in venue_slots[index:]:
                if _slot_overlaps(slot_a, slot_b):
                    vars_b = by_venue_slot[(venue_id, slot_b.slot_id)]
                    if slot_a.slot_id == slot_b.slot_id:
                        model.Add(sum(vars_a) <= 1)
                    else:
                        model.Add(sum(vars_a) + sum(vars_b) <= 1)

    invigilator_by_id = {inv.invigilator_id: inv for inv in invigilators}
    for invigilator_id, variables in staff_by_invigilator.items():
        maximum = invigilator_by_id[invigilator_id].max_duties
        if maximum is not None:
            model.Add(sum(variables) <= maximum)
        inv_slots = [slot for slot in slots if staff_by_invigilator_slot.get((invigilator_id, slot.slot_id))]
        for index, slot_a in enumerate(inv_slots):
            vars_a = staff_by_invigilator_slot[(invigilator_id, slot_a.slot_id)]
            for slot_b in inv_slots[index:]:
                if _slot_overlaps(slot_a, slot_b):
                    vars_b = staff_by_invigilator_slot[(invigilator_id, slot_b.slot_id)]
                    if slot_a.slot_id == slot_b.slot_id:
                        model.Add(sum(vars_a) <= 1)
                    else:
                        model.Add(sum(vars_a) + sum(vars_b) <= 1)

    used_slots = {}
    for slot_id, variables in by_slot.items():
        used = model.NewBoolVar(f"slot_used_{slot_id}")
        used_slots[slot_id] = used
        model.Add(sum(variables) >= used)
        for variable in variables:
            model.Add(variable <= used)
    maximum_slack = len(courses) * max((venue.capacity for venue in venues), default=1)
    slot_weight = maximum_slack + 1
    course_by_id = {course.course_id: course for course in courses}
    venue_by_id = {venue.venue_id: venue for venue in venues}
    room_slack = [
        (venue_by_id[venue_id].capacity - student_counts[course_id]) * variable
        for (course_id, _, venue_id), variable in assignments.items()
    ]
    model.Minimize(
        slot_weight * sum(used_slots.values()) +
        sum(room_slack)
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    started = time.perf_counter()
    status = solver.Solve(model)
    runtime = time.perf_counter() - started
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError(
            "No feasible timetable satisfies the student, venue, duration, availability, "
            "qualification, and workload constraints. Check the input data or add resources."
        )

    selected = []
    for (course_id, slot_id, venue_id), variable in assignments.items():
        if not solver.Value(variable):
            continue
        exam = ScheduledExam(
            course_id=course_id, slot_id=slot_id, venue_id=venue_id,
            student_count=student_counts[course_id]
        )
        selected.append((exam, (course_id, slot_id, venue_id)))

    db.session.query(InvigilatorDuty).delete()
    db.session.query(ScheduledExam).delete()
    db.session.flush()
    for exam, key in selected:
        db.session.add(exam)
    db.session.flush()
    selected_keys = {key for _, key in selected}
    for key, variable in staffing.items():
        if key[:3] in selected_keys and solver.Value(variable):
            exam = next(exam for exam, selected_key in selected if selected_key == key[:3])
            db.session.add(InvigilatorDuty(exam_id=exam.id, invigilator_id=key[3]))
    duty_count = sum(1 for variable in staffing.values() if solver.Value(variable))
    used_slot_count = len({key[1] for _, key in selected})
    run = OptimizationRun(
        solver_status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        runtime_seconds=runtime, courses_scheduled=len(selected),
        conflict_pairs=len(conflict_pairs), slots_used=used_slot_count,
        duties_assigned=duty_count
    )
    db.session.add(run)
    db.session.commit()
    return {"status": run.solver_status, "runtime": runtime}


def _database_url(instance_path):
    configured_url = os.environ.get("DATABASE_URL")
    if configured_url:
        return configured_url

    mysql_database = os.environ.get("MYSQL_DATABASE")
    if mysql_database:
        return URL.create(
            "mysql+mysqlconnector",
            username=os.environ.get("MYSQL_USER") or None,
            password=os.environ.get("MYSQL_PASSWORD") or None,
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            database=mysql_database,
            query={"charset": "utf8mb4"},
        )
    return f"sqlite:///{(Path(instance_path) / 'examinations.sqlite3').as_posix()}"


def _ensure_mysql_database(database_uri):
    url = make_url(database_uri)
    if url.drivername != "mysql+mysqlconnector":
        return
    database_name = url.database
    if not database_name or not re.fullmatch(r"[A-Za-z0-9_]+", database_name):
        raise ValueError("MYSQL_DATABASE must contain only letters, numbers, and underscores.")

    connection = mysql.connector.connect(
        host=url.host, port=url.port or 3306,
        user=url.username, password=url.password,
        connection_timeout=5, charset="utf8mb4",
    )
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
            (database_name,),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"CREATE DATABASE `{database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


def _migrate_examiner_account_columns():
    columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("examiner_account")
    }
    with db.engine.begin() as connection:
        if "is_admin" not in columns:
            connection.execute(text(
                "ALTER TABLE examiner_account "
                "ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "is_approved" not in columns:
            connection.execute(text(
                "ALTER TABLE examiner_account "
                "ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT 0"
            ))
            connection.execute(text(
                "UPDATE examiner_account SET is_approved = 1"
            ))
        if "password_reset_nonce" not in columns:
            connection.execute(text(
                "ALTER TABLE examiner_account "
                "ADD COLUMN password_reset_nonce VARCHAR(64) NULL"
            ))


def _bootstrap_admin(app):
    email = os.environ.get("EXAMINER_ADMIN_EMAIL", "").strip().casefold()
    password = os.environ.get("EXAMINER_ADMIN_PASSWORD", "")
    if not email and not password:
        app.logger.warning(
            "No examiner administrator configured; set EXAMINER_ADMIN_EMAIL and "
            "EXAMINER_ADMIN_PASSWORD to enable approval management."
        )
        return
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("EXAMINER_ADMIN_EMAIL must be a valid email address.")
    if len(password) < 12 or len(password) > 128:
        raise ValueError("EXAMINER_ADMIN_PASSWORD must contain 12 to 128 characters.")

    account = ExaminerAccount.query.filter_by(email=email).first()
    if account is None:
        db.session.add(ExaminerAccount(
            full_name=os.environ.get("EXAMINER_ADMIN_NAME", "Examinations Administrator").strip(),
            email=email, department="Examinations Administration",
            password_hash=generate_password_hash(password),
            is_admin=True, is_approved=True,
        ))
    else:
        account.is_admin = True
        account.is_approved = True
    db.session.commit()


def _send_password_reset_email(account, reset_url):
    smtp_host = current_app.config["SMTP_HOST"]
    sender = current_app.config["SMTP_FROM"] or current_app.config["SMTP_USER"]
    if not smtp_host or not sender:
        raise RuntimeError("Password reset email is not configured.")

    message = EmailMessage()
    message["Subject"] = "Reset your University of Mkar examiner password"
    message["From"] = sender
    message["To"] = account.email
    message.set_content(
        f"Hello {account.full_name},\n\n"
        "A password reset was requested for your examiner account. "
        "Use this link within the next hour:\n\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )

    with smtplib.SMTP(
        smtp_host, current_app.config["SMTP_PORT"], timeout=10
    ) as server:
        if current_app.config["SMTP_USE_TLS"]:
            server.starttls(context=ssl.create_default_context())
        smtp_user = current_app.config["SMTP_USER"]
        if smtp_user:
            server.login(smtp_user, current_app.config["SMTP_PASSWORD"])
        server.send_message(message)


def _password_reset_account(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        payload = serializer.loads(
            token, salt="examiner-password-reset",
            max_age=current_app.config["PASSWORD_RESET_MAX_AGE"],
        )
    except BadSignature:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
        return None
    account = db.session.get(ExaminerAccount, payload["id"])
    if (account is None or not account.is_approved or not account.password_reset_nonce or
            not secrets.compare_digest(account.password_reset_nonce, str(payload.get("nonce", "")))):
        return None
    return account


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    production_mode = os.environ.get("APP_ENV", "development").casefold() == "production"
    secret_key = os.environ.get("SECRET_KEY")
    secure_session_cookie = os.environ.get("SESSION_COOKIE_SECURE", "false").casefold() in {"1", "true", "yes"}
    if production_mode:
        if not secret_key or len(secret_key) < 32:
            raise RuntimeError("Production requires a persistent SECRET_KEY of at least 32 characters.")
        if not secure_session_cookie:
            raise RuntimeError("Production requires SESSION_COOKIE_SECURE=true and HTTPS.")
    app.config.update(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=_database_url(app.instance_path),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=secure_session_cookie,
        SESSION_COOKIE_SAMESITE="Lax",
        WTF_CSRF_ENABLED=not bool((test_config or {}).get("TESTING", False)),
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USER=os.environ.get("SMTP_USER", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_FROM=os.environ.get("SMTP_FROM", ""),
        SMTP_USE_TLS=os.environ.get("SMTP_USE_TLS", "true").casefold() in {"1", "true", "yes"},
        PASSWORD_RESET_MAX_AGE=3600,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    login_manager.login_view = "login"
    login_manager.init_app(app)
    csrf.init_app(app)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    with app.app_context():
        _ensure_mysql_database(app.config["SQLALCHEMY_DATABASE_URI"])
        db.create_all()
        _migrate_examiner_account_columns()
        _bootstrap_admin(app)
    @app.get("/")
    @login_required
    def dashboard():
        if current_user.is_admin:
            return redirect(url_for("examiner_approvals"))
        exams = ScheduledExam.query.join(Course).join(ExamSlot).order_by(
            ExamSlot.date, ExamSlot.start_time, Course.course_code
        ).all()
        stats = {
            "students": Student.query.count(), "courses": Course.query.count(),
            "venues": Venue.query.count(), "invigilators": Invigilator.query.count(),
            "scheduled": len(exams), "duties": InvigilatorDuty.query.count(),
        }
        latest_run = OptimizationRun.query.order_by(OptimizationRun.created_at.desc()).first()
        solver_ready = True
        try:
            import ortools  # noqa: F401
        except ImportError:
            solver_ready = False
        return render_template(
            "dashboard.html", stats=stats, exams=exams,
            solver_ready=solver_ready, latest_run=latest_run
        )

    @app.get("/admin/examiners")
    @login_required
    def examiner_approvals():
        if not current_user.is_admin:
            abort(403)
        view = request.args.get("view", "pending")
        query = ExaminerAccount.query.order_by(ExaminerAccount.created_at, ExaminerAccount.email)
        if view == "approved":
            accounts = query.filter_by(is_approved=True).all()
        elif view == "all":
            accounts = query.all()
        else:
            view = "pending"
            accounts = query.filter_by(is_approved=False).all()
        pending_count = ExaminerAccount.query.filter_by(is_approved=False).count()
        return render_template(
            "admin_examiners.html", accounts=accounts,
            pending_count=pending_count, view=view,
        )

    @app.post("/admin/examiners/<int:account_id>/approve")
    @login_required
    def approve_examiner(account_id):
        if not current_user.is_admin:
            abort(403)
        account = db.session.get(ExaminerAccount, account_id)
        if account is None:
            abort(404)
        account.is_approved = True
        db.session.commit()
        flash(f"{account.full_name} is approved to sign in.", "success")
        return redirect(url_for("examiner_approvals"))

    @app.post("/admin/examiners/<int:account_id>/reject")
    @login_required
    def reject_examiner(account_id):
        if not current_user.is_admin:
            abort(403)
        account = db.session.get(ExaminerAccount, account_id)
        if account is None:
            abort(404)
        if account.is_admin or account.is_approved:
            flash("Only pending non-administrator applications can be rejected.", "error")
            return redirect(url_for("examiner_approvals"))
        db.session.delete(account)
        db.session.commit()
        flash("Examiner application rejected.", "success")
        return redirect(url_for("examiner_approvals"))

    def department_suggestions():
        return sorted({
            department.strip()
            for department, in db.session.query(Invigilator.department).distinct().all()
            if department and department.strip()
        }, key=str.casefold)

    def safe_next_url(target):
        if not target:
            return url_for("dashboard")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != request.host.casefold():
                return url_for("dashboard")
        elif not target.startswith("/") or target.startswith("//"):
            return url_for("dashboard")
        return target

    def admin_setup_available():
        return ExaminerAccount.query.filter_by(
            is_admin=True, is_approved=True
        ).first() is None

    @app.route("/setup-admin", methods=["GET", "POST"])
    def setup_admin_page():
        if not admin_setup_available():
            abort(404)
        values = request.form if request.method == "POST" else {}
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().casefold()
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            error = None
            if not full_name or len(full_name) > 150:
                error = "Enter your name (up to 150 characters)."
            elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
                error = "Enter a valid email address."
            elif len(password) < 12 or len(password) > 128:
                error = "Password must contain between 12 and 128 characters."
            elif password != confirmation:
                error = "The passwords do not match."

            if error:
                flash(error, "error")
            else:
                account = ExaminerAccount.query.filter_by(email=email).first()
                if account is None:
                    account = ExaminerAccount(email=email)
                    db.session.add(account)
                account.full_name = full_name
                account.department = "Examinations Administration"
                account.password_hash = generate_password_hash(password)
                account.is_admin = True
                account.is_approved = True
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("That email is already registered. Try signing in.", "error")
                else:
                    login_user(account)
                    flash("Administrator account created.", "success")
                    return redirect(url_for("dashboard"))
        return render_template("admin_setup.html", values=values)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        departments = department_suggestions()
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().casefold()
            department = request.form.get("department", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            error = None
            if not full_name or len(full_name) > 150:
                error = "Enter your name (up to 150 characters)."
            elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
                error = "Enter a valid email address."
            elif not department or len(department) > 150:
                error = "Enter your department (up to 150 characters)."
            elif len(password) < 10 or len(password) > 128:
                error = "Password must contain between 10 and 128 characters."
            elif password != confirmation:
                error = "The passwords do not match."
            elif ExaminerAccount.query.filter_by(email=email).first() is not None:
                error = "An account with that email already exists."

            if error:
                flash(error, "error")
            else:
                account = ExaminerAccount(
                    full_name=full_name, email=email, department=department,
                    password_hash=generate_password_hash(password),
                    is_admin=False, is_approved=False,
                )
                try:
                    db.session.add(account)
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("An account with that email already exists.", "error")
                else:
                    flash(
                        "Registration received. You can sign in after an administrator approves your account.",
                        "success",
                    )
                    return redirect(url_for("login"))
            return render_template(
                "auth.html", mode="register", departments=departments,
                values=request.form,
            )
        return render_template("auth.html", mode="register", departments=departments, values={})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        next_target = request.args.get("next", "")
        if request.method == "POST":
            email = request.form.get("email", "").strip().casefold()
            password = request.form.get("password", "")
            account = ExaminerAccount.query.filter_by(email=email).first()
            if account is not None and check_password_hash(account.password_hash, password):
                if not account.is_approved:
                    flash("Your registration is awaiting administrator approval.", "error")
                else:
                    login_user(account)
                    return redirect(safe_next_url(request.form.get("next") or next_target))
            else:
                flash("Email or password is incorrect.", "error")
        return render_template(
            "auth.html", mode="login", next_url=next_target, values=request.form,
            admin_setup_available=admin_setup_available(),
        )

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = request.form.get("email", "").strip().casefold()
            account = ExaminerAccount.query.filter_by(
                email=email, is_approved=True
            ).first()
            if account is not None:
                account.password_reset_nonce = secrets.token_urlsafe(32)
                db.session.commit()
                serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
                token = serializer.dumps(
                    {"id": account.id, "nonce": account.password_reset_nonce},
                    salt="examiner-password-reset",
                )
                reset_url = url_for("reset_password", token=token, _external=True)
                try:
                    _send_password_reset_email(account, reset_url)
                except (OSError, RuntimeError, smtplib.SMTPException):
                    current_app.logger.exception("Could not send examiner password reset email.")
            flash(
                "If an approved examiner account matches that email, reset instructions have been sent.",
                "success",
            )
            return redirect(url_for("forgot_password"))
        return render_template("password_reset.html", mode="request", values={})

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        account = _password_reset_account(token)
        if account is None:
            flash("That password reset link is invalid or has expired.", "error")
            return redirect(url_for("forgot_password"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            if len(password) < 12 or len(password) > 128:
                flash("Password must contain between 12 and 128 characters.", "error")
            elif password != confirmation:
                flash("The passwords do not match.", "error")
            else:
                account.password_hash = generate_password_hash(password)
                account.password_reset_nonce = None
                db.session.commit()
                flash("Password updated. You can now sign in.", "success")
                return redirect(url_for("login"))
        return render_template("password_reset.html", mode="reset", values={})

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    @app.get("/health/database")
    def database_health():
        connection = None
        cursor = None
        try:
            if db.engine.dialect.name == "mysql":
                url = db.engine.url
                connection = mysql.connector.connect(
                    host=url.host,
                    port=url.port or 3306,
                    user=url.username,
                    password=url.password,
                    database=url.database,
                    connection_timeout=5,
                    charset="utf8mb4",
                )
                cursor = connection.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
            else:
                db.session.execute(text("SELECT 1"))
            return jsonify({"status": "ok", "database": db.engine.dialect.name})
        except (mysql.connector.Error, SQLAlchemyError, OSError):
            return jsonify({"status": "error", "message": "Database connection failed; check database settings and server status."}), 503
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

    @app.get("/records")
    @login_required
    def records_home():
        return redirect(url_for("list_records", table_name="students"))

    @app.get("/records/<table_name>")
    @login_required
    def list_records(table_name):
        if table_name not in RECORD_TABLES:
            abort(404)
        config = _record_form_fields(table_name)
        records = config["model"].query.order_by(*[
            getattr(config["model"], key) for key in config["keys"]
        ]).all()
        table_navigation = {
            key: {"label": values[1]} for key, values in RECORD_TABLES.items()
        }
        record_rows = [{
            "values": {field: getattr(record, field) for field in TABLE_FIELDS[table_name]},
            "key": _record_key(record, config["keys"]),
        } for record in records]
        return render_template(
            "records.html", view="list", table_name=table_name,
            config=config, tables=table_navigation, records=record_rows, values={},
        )

    def render_record_form(table_name, view, values):
        return render_template(
            "records.html", view=view, table_name=table_name,
            config=_record_form_fields(table_name),
            tables={key: {"label": item[1]} for key, item in RECORD_TABLES.items()},
            records=[], values=values,
        )

    @app.route("/records/<table_name>/new", methods=["GET", "POST"])
    @login_required
    def new_record(table_name):
        if table_name not in RECORD_TABLES:
            abort(404)
        config = _record_form_fields(table_name)
        if request.method == "GET":
            defaults = {"available": True, "qualified_courses": "*"}
            return render_record_form(table_name, "new", defaults)
        try:
            values = _parse_record_values(table_name, request.form)
            db.session.add(config["model"](**values))
            _invalidate_timetable()
            db.session.commit()
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
            return render_record_form(table_name, "new", request.form)
        except IntegrityError:
            db.session.rollback()
            flash("Record could not be created. Check for a duplicate ID or key.", "error")
            return render_record_form(table_name, "new", request.form)
        flash("Record created.", "success")
        return redirect(url_for("list_records", table_name=table_name))

    @app.route("/records/<table_name>/<record_key>/edit", methods=["GET", "POST"])
    @login_required
    def edit_record(table_name, record_key):
        if table_name not in RECORD_TABLES:
            abort(404)
        config = _record_form_fields(table_name)
        primary_key = _parse_record_key(record_key, config["keys"])
        record = db.session.get(config["model"], primary_key)
        if record is None:
            abort(404)
        if request.method == "GET":
            values = {field: getattr(record, field) for field in TABLE_FIELDS[table_name]}
            return render_record_form(table_name, "edit", values)
        try:
            values = _parse_record_values(table_name, request.form)
            for field, value in values.items():
                if field in config["keys"] and not config["allow_key_edit"]:
                    continue
                setattr(record, field, value)
            _invalidate_timetable()
            db.session.commit()
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
            return render_record_form(table_name, "edit", request.form)
        except IntegrityError:
            db.session.rollback()
            flash("Record could not be updated. Check for duplicate keys and valid references.", "error")
            return render_record_form(table_name, "edit", request.form)
        flash("Record updated.", "success")
        return redirect(url_for("list_records", table_name=table_name))

    @app.post("/records/<table_name>/<record_key>/delete")
    @login_required
    def delete_record(table_name, record_key):
        if table_name not in RECORD_TABLES:
            abort(404)
        config = _record_form_fields(table_name)
        primary_key = _parse_record_key(record_key, config["keys"])
        record = db.session.get(config["model"], primary_key)
        if record is None:
            abort(404)

        has_dependents = False
        if table_name == "students":
            has_dependents = Registration.query.filter_by(student_id=record.student_id).first() is not None
        elif table_name == "courses":
            has_dependents = (
                Registration.query.filter_by(course_id=record.course_id).first() is not None or
                ScheduledExam.query.filter_by(course_id=record.course_id).first() is not None or
                any(record.course_id in {
                        course_id.strip() for course_id in (inv.qualified_courses or "").split(";")
                    }
                    for inv in Invigilator.query.all())
            )
        elif table_name == "slots":
            has_dependents = (
                Availability.query.filter_by(slot_id=record.slot_id).first() is not None or
                ScheduledExam.query.filter_by(slot_id=record.slot_id).first() is not None
            )
        elif table_name == "venues":
            has_dependents = ScheduledExam.query.filter_by(venue_id=record.venue_id).first() is not None
        elif table_name == "invigilators":
            has_dependents = (
                Availability.query.filter_by(invigilator_id=record.invigilator_id).first() is not None or
                InvigilatorDuty.query.filter_by(invigilator_id=record.invigilator_id).first() is not None
            )
        if has_dependents:
            flash("Delete dependent registrations, availability, or timetable records first.", "error")
            return redirect(url_for("list_records", table_name=table_name))

        try:
            db.session.delete(record)
            _invalidate_timetable()
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Record is still referenced by other data and cannot be deleted.", "error")
            return redirect(url_for("list_records", table_name=table_name))
        flash("Record deleted.", "success")
        return redirect(url_for("list_records", table_name=table_name))

    @app.post("/optimize")
    @login_required
    def optimize():
        try:
            required = int(request.form.get("required_per_exam", "2"))
            limit = int(request.form.get("time_limit_seconds", "20"))
            if not 1 <= required <= 10 or not 1 <= limit <= 120:
                raise ValueError("Invigilators per exam must be 1-10 and solve time must be 1-120 seconds.")
            result = optimize_schedule(required, limit)
        except (ValueError, RuntimeError) as error:
            db.session.rollback()
            flash(str(error), "error")
            return redirect(url_for("dashboard"))
        flash(f"Schedule {result['status'].lower()} in {result['runtime']:.2f}s.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/timetable.xlsx")
    @login_required
    def export_timetable():
        exams = ScheduledExam.query.join(Course).join(ExamSlot).order_by(
            ExamSlot.date, ExamSlot.start_time, Course.course_code
        ).all()
        if not exams:
            flash("Generate a timetable before exporting.", "error")
            return redirect(url_for("dashboard"))
        workbook = Workbook()
        timetable = workbook.active
        timetable.title = "Timetable"
        timetable.append(("Date", "Start", "End", "Course code", "Course title", "Slot", "Venue", "Students", "Invigilators"))
        roster = workbook.create_sheet("Invigilator roster")
        roster.append(("Date", "Start", "End", "Course code", "Course title", "Venue", "Invigilator ID", "Invigilator name"))
        for exam in exams:
            names = "; ".join(duty.invigilator.name for duty in exam.duties)
            timetable.append((
                exam.slot.date, exam.slot.start_time, exam.slot.end_time,
                exam.course.course_code, exam.course.course_title, exam.slot.slot_id,
                exam.venue.venue_name, exam.student_count, names
            ))
            for duty in exam.duties:
                roster.append((
                    exam.slot.date, exam.slot.start_time, exam.slot.end_time,
                    exam.course.course_code, exam.course.course_title,
                    exam.venue.venue_name, duty.invigilator.invigilator_id,
                    duty.invigilator.name
                ))
        for sheet in workbook.worksheets:
            for cell in sheet[1]:
                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = PatternFill("solid", fgColor="176B5B")
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                letter = column[0].column_letter
                sheet.column_dimensions[letter].width = min(
                    max(14, max(len(str(cell.value or "")) for cell in column) + 2), 36
                )
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(
            output, as_attachment=True, download_name="university_exam_timetable.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    return app


app = create_app()

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)