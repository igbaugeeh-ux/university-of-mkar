import unittest
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from unittest.mock import patch

from flask import Flask
from sqlalchemy import URL, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as application


class FlaskWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.web_app = application.create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
        })
        self.client = self.web_app.test_client()
        with self.web_app.app_context():
            account = application.ExaminerAccount(
                full_name="Test Examiner", email="fixture@example.edu",
                department="Computer Science",
                password_hash=application.generate_password_hash("FixturePass123"),
                is_admin=True, is_approved=True,
            )
            application.db.session.add(account)
            application.db.session.commit()
        response = self.client.post("/login", data={
            "email": "fixture@example.edu", "password": "FixturePass123",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        with self.web_app.app_context():
            application.db.session.remove()
            application.db.drop_all()

    def test_dashboard_uses_record_forms_for_data_entry(self):
        with self.web_app.app_context():
            account = application.ExaminerAccount(
                full_name="Regular Examiner", email="regular@example.edu",
                department="Computer Science",
                password_hash=application.generate_password_hash("RegularPass123"),
                is_approved=True,
            )
            application.db.session.add(account)
            application.db.session.commit()
        client = self.web_app.test_client()
        client.post("/login", data={
            "email": "regular@example.edu", "password": "RegularPass123",
        })

        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Examination schedule", response.data)
        self.assertIn(b"Enter exam data", response.data)
        self.assertIn(b"Open record manager", response.data)
        self.assertNotIn(b"Import records", response.data)

        with self.web_app.app_context():
            self.assertEqual(application.Student.query.count(), 0)
            self.assertEqual(application.Course.query.count(), 0)

        self.assertEqual(client.post("/upload").status_code, 404)
        self.assertEqual(client.get("/template.xlsx").status_code, 404)
        for table_name in application.TABLE_FIELDS:
            response = client.get(f"/records/{table_name}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Add record", response.data)

    def test_admin_home_redirects_to_admin_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/examiners")

    def test_optimizer_form_validates_before_solving(self):
        with patch.object(application, "optimize_schedule") as optimize:
            response = self.client.post(
                "/optimize",
                data={"required_per_exam": "0", "time_limit_seconds": "20"},
                follow_redirects=True,
            )
        self.assertIn(b"Invigilators per exam must be 1-10", response.data)
        optimize.assert_not_called()

    def test_database_health_and_mysql_url_configuration(self):
        response = self.client.get("/health/database")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["database"], "sqlite")

        mysql_settings = {
            "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3307",
            "MYSQL_USER": "examadmin", "MYSQL_PASSWORD": "test-pass",
            "MYSQL_DATABASE": "mkar_exams",
        }
        with patch.dict(os.environ, mysql_settings, clear=True):
            url = application._database_url(self.web_app.instance_path)
        self.assertEqual(url.drivername, "mysql+mysqlconnector")
        self.assertEqual(url.host, "127.0.0.1")
        self.assertEqual(url.port, 3307)
        self.assertEqual(url.database, "mkar_exams")

    def test_security_headers_and_https_cookie_setting(self):
        response = self.client.get("/health/database")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(
            response.headers["Permissions-Policy"],
            "camera=(), microphone=(), geolocation=()",
        )

        with patch.dict(os.environ, {"SESSION_COOKIE_SECURE": "true"}):
            secure_app = application.create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
            })
        self.assertTrue(secure_app.config["SESSION_COOKIE_SECURE"])

    def test_production_requires_persistent_secret_and_secure_cookie(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "SECRET_KEY": "",
            "SESSION_COOKIE_SECURE": "true",
        }):
            with self.assertRaisesRegex(RuntimeError, "persistent SECRET_KEY"):
                application.create_app({"TESTING": True})

        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "SECRET_KEY": "test-secret-that-is-at-least-32-characters-long",
            "SESSION_COOKIE_SECURE": "false",
        }):
            with self.assertRaisesRegex(RuntimeError, "SESSION_COOKIE_SECURE=true"):
                application.create_app({"TESTING": True})

    def test_mysql_schema_connection_preserves_url_password(self):
        connection = unittest.mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = ("mkar_examinations",)
        database_url = URL.create(
            "mysql+mysqlconnector", username="root", password="local-test-password",
            host="127.0.0.1", port=3306, database="mkar_examinations",
        )
        with patch.object(
            application.mysql.connector, "connect", return_value=connection
        ) as connect:
            application._ensure_mysql_database(database_url)
        self.assertEqual(connect.call_args.kwargs["password"], "local-test-password")
        connection.close.assert_called_once()

    def test_course_record_create_read_update_delete(self):
        response = self.client.get("/records/courses")
        self.assertEqual(response.status_code, 200)

        response = self.client.post("/records/courses/new", data={
            "course_id": "C999", "course_code": "TST999",
            "course_title": "CRUD Test", "duration_minutes": "60",
        })
        self.assertEqual(response.status_code, 302)
        with self.web_app.app_context():
            course = application.db.session.get(application.Course, "C999")
            self.assertIsNotNone(course)
            record_key = application._record_key(course, ("course_id",))

        response = self.client.get(f"/records/courses/{record_key}/edit")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f"/records/courses/{record_key}/edit", data={
            "course_id": "C999", "course_code": "TST998",
            "course_title": "Updated CRUD Test", "duration_minutes": "90",
        })
        self.assertEqual(response.status_code, 302)
        with self.web_app.app_context():
            course = application.db.session.get(application.Course, "C999")
            self.assertEqual(course.course_code, "TST998")
            self.assertEqual(course.duration_minutes, 90)
            record_key = application._record_key(course, ("course_id",))

        response = self.client.post(f"/records/courses/{record_key}/delete")
        self.assertEqual(response.status_code, 302)
        with self.web_app.app_context():
            self.assertIsNone(application.db.session.get(application.Course, "C999"))

    def test_registration_rejects_unknown_foreign_keys(self):
        response = self.client.post("/records/registrations/new", data={
            "student_id": "UNKNOWN", "course_id": "C001",
        }, follow_redirects=True)
        self.assertIn(b"Choose an existing student ID", response.data)

    def test_examiner_registration_login_and_logout(self):
        client = self.web_app.test_client()
        response = client.get("/register")
        self.assertEqual(response.status_code, 200)
        response = client.post("/register", data={
            "full_name": "Ada Examiner", "email": "ADA@EXAMPLE.EDU",
            "department": "Mathematics", "password": "SecureExaminer123",
            "confirm_password": "SecureExaminer123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with self.web_app.app_context():
            account = application.ExaminerAccount.query.filter_by(
                email="ada@example.edu"
            ).one()
            self.assertNotEqual(account.password_hash, "SecureExaminer123")
            self.assertTrue(application.check_password_hash(
                account.password_hash, "SecureExaminer123"
            ))
            self.assertFalse(account.is_approved)
            account_id = account.id
        pending_login = client.post("/login", data={
            "email": "ada@example.edu", "password": "SecureExaminer123",
        }, follow_redirects=True)
        self.assertIn(b"awaiting administrator approval", pending_login.data)
        self.assertEqual(client.get("/").status_code, 302)

        approval = self.client.post(f"/admin/examiners/{account_id}/approve")
        self.assertEqual(approval.status_code, 302)
        approved_login = client.post("/login", data={
            "email": "ada@example.edu", "password": "SecureExaminer123",
        })
        self.assertEqual(approved_login.status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)

        client.post("/logout")
        self.assertEqual(client.get("/").status_code, 302)
        response = client.post("/login", data={
            "email": "ada@example.edu", "password": "SecureExaminer123",
            "next": "https://attacker.example/",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_password_reset_is_generic_and_changes_password(self):
        with self.web_app.app_context():
            account = application.ExaminerAccount(
                full_name="Reset Examiner", email="reset@example.edu",
                department="Physics",
                password_hash=application.generate_password_hash("OldExaminerPass123"),
                is_approved=True,
            )
            application.db.session.add(account)
            application.db.session.commit()

        client = self.web_app.test_client()
        with patch.object(application, "_send_password_reset_email") as send_email:
            response = client.post("/forgot-password", data={
                "email": "missing@example.edu",
            }, follow_redirects=True)
            generic_message = b"If an approved examiner account matches that email"
            self.assertIn(generic_message, response.data)
            send_email.assert_not_called()

            response = client.post("/forgot-password", data={
                "email": "reset@example.edu",
            }, follow_redirects=True)
            self.assertIn(generic_message, response.data)
            send_email.assert_called_once()
            reset_url = send_email.call_args.args[1]

        reset_path = urlsplit(reset_url).path
        self.assertEqual(client.get(reset_path).status_code, 200)
        response = client.post(reset_path, data={
            "password": "NewExaminerPass123",
            "confirm_password": "NewExaminerPass123",
        })
        self.assertEqual(response.status_code, 302)
        reused_link = client.get(reset_path, follow_redirects=True)
        self.assertIn(b"invalid or has expired", reused_link.data)
        old_login = client.post("/login", data={
            "email": "reset@example.edu", "password": "OldExaminerPass123",
        }, follow_redirects=True)
        self.assertIn(b"Email or password is incorrect", old_login.data)
        new_login = client.post("/login", data={
            "email": "reset@example.edu", "password": "NewExaminerPass123",
        })
        self.assertEqual(new_login.status_code, 302)

    def test_approval_queue_and_rejection(self):
        with self.web_app.app_context():
            pending = application.ExaminerAccount(
                full_name="Pending Examiner", email="pending@example.edu",
                department="Science",
                password_hash=application.generate_password_hash("PendingPass123"),
            )
            application.db.session.add(pending)
            application.db.session.commit()
            pending_id = pending.id

        response = self.client.get("/admin/examiners")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pending Examiner", response.data)
        response = self.client.post(f"/admin/examiners/{pending_id}/reject")
        self.assertEqual(response.status_code, 302)
        with self.web_app.app_context():
            self.assertIsNone(application.db.session.get(
                application.ExaminerAccount, pending_id
            ))

    def test_non_admin_cannot_view_approval_queue(self):
        with self.web_app.app_context():
            examiner = application.ExaminerAccount(
                full_name="Department Examiner", email="dept@example.edu",
                department="Physics",
                password_hash=application.generate_password_hash("DeptPass12345"),
                is_approved=True, is_admin=False,
            )
            application.db.session.add(examiner)
            application.db.session.commit()
        client = self.web_app.test_client()
        client.post("/login", data={
            "email": "dept@example.edu", "password": "DeptPass12345",
        })
        self.assertEqual(client.get("/admin/examiners").status_code, 403)

    def test_bootstrap_admin_from_environment(self):
        settings = {
            "EXAMINER_ADMIN_EMAIL": "bootstrap@example.edu",
            "EXAMINER_ADMIN_PASSWORD": "BootstrapSecure123",
            "EXAMINER_ADMIN_NAME": "Bootstrap Admin",
        }
        with patch.dict(os.environ, settings, clear=False):
            with self.web_app.app_context():
                application._bootstrap_admin(self.web_app)
                admin = application.ExaminerAccount.query.filter_by(
                    email="bootstrap@example.edu"
                ).one()
                self.assertTrue(admin.is_admin)
                self.assertTrue(admin.is_approved)
                self.assertTrue(application.check_password_hash(
                    admin.password_hash, settings["EXAMINER_ADMIN_PASSWORD"]
                ))

    def test_existing_account_is_preserved_by_schema_migration(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            database_uri = f"sqlite:///{database_path.as_posix()}"
            legacy_app = Flask("legacy-account-database")
            legacy_app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
            application.db.init_app(legacy_app)
            with legacy_app.app_context():
                with application.db.engine.begin() as connection:
                    connection.execute(text(
                        "CREATE TABLE examiner_account ("
                        "id INTEGER PRIMARY KEY, full_name VARCHAR(150) NOT NULL, "
                        "email VARCHAR(254) NOT NULL UNIQUE, department VARCHAR(150) NOT NULL, "
                        "password_hash VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL)"
                    ))
                    connection.execute(text(
                        "INSERT INTO examiner_account "
                        "(id, full_name, email, department, password_hash, created_at) "
                        "VALUES (1, 'Existing Examiner', 'existing@example.edu', "
                        "'Registry', 'hashed-password', '2026-01-01 00:00:00')"
                    ))
                legacy_engine = application.db.engine

            upgraded_app = application.create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": database_uri,
            })
            with upgraded_app.app_context():
                existing = application.ExaminerAccount.query.filter_by(
                    email="existing@example.edu"
                ).one()
                self.assertTrue(existing.is_approved)
                self.assertFalse(existing.is_admin)
                upgraded_engine = application.db.engine
            legacy_engine.dispose()
            upgraded_engine.dispose()

    def test_login_form_rejects_missing_csrf_token(self):
        csrf_app = application.create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
        })
        response = csrf_app.test_client().post("/login", data={
            "email": "nobody@example.edu", "password": "SomePassword123",
        })
        self.assertEqual(response.status_code, 400)
        with csrf_app.app_context():
            application.db.session.remove()
            application.db.drop_all()

    def test_first_admin_setup_is_available_once(self):
        setup_app = application.create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
        })
        client = setup_app.test_client()
        self.assertEqual(client.get("/setup-admin").status_code, 200)
        response = client.post("/setup-admin", data={
            "full_name": "Initial Administrator",
            "email": "initial-admin@example.edu",
            "password": "InitialAdminSecure123",
            "confirm_password": "InitialAdminSecure123",
        })
        self.assertEqual(response.status_code, 302)
        with setup_app.app_context():
            account = application.ExaminerAccount.query.filter_by(
                email="initial-admin@example.edu"
            ).one()
            self.assertTrue(account.is_admin)
            self.assertTrue(account.is_approved)
            self.assertTrue(application.check_password_hash(
                account.password_hash, "InitialAdminSecure123"
            ))
        home_response = client.get("/")
        self.assertEqual(home_response.status_code, 302)
        self.assertEqual(home_response.headers["Location"], "/admin/examiners")
        self.assertEqual(client.get("/admin/examiners").status_code, 200)
        self.assertEqual(client.get("/setup-admin").status_code, 404)
        with setup_app.app_context():
            application.db.session.remove()
            application.db.drop_all()

    def test_composite_registration_key_can_be_updated(self):
        with self.web_app.app_context():
            application.db.session.add_all([
                application.Student(
                    student_id="S004", matric_number="MAT004",
                    programme="Computer Science", level="400",
                ),
                application.Course(
                    course_id="C001", course_code="CSC101",
                    course_title="Introduction", duration_minutes=120,
                ),
                application.Course(
                    course_id="C003", course_code="CSC303",
                    course_title="Algorithms", duration_minutes=120,
                ),
            ])
            application.db.session.commit()
        response = self.client.post("/records/registrations/new", data={
            "student_id": "S004", "course_id": "C003",
        })
        self.assertEqual(response.status_code, 302)
        with self.web_app.app_context():
            registration = application.db.session.get(
                application.Registration, ("S004", "C003")
            )
            record_key = application._record_key(
                registration, ("student_id", "course_id")
            )

        response = self.client.post(
            f"/records/registrations/{record_key}/edit",
            data={"student_id": "S004", "course_id": "C001"},
        )
        self.assertEqual(response.status_code, 302)
        with self.web_app.app_context():
            self.assertIsNone(application.db.session.get(
                application.Registration, ("S004", "C003")
            ))
            self.assertIsNotNone(application.db.session.get(
                application.Registration, ("S004", "C001")
            ))

    def test_delete_is_blocked_when_course_has_registrations(self):
        with self.web_app.app_context():
            course = application.Course(
                course_id="C001", course_code="CSC101",
                course_title="Introduction", duration_minutes=120,
            )
            student = application.Student(
                student_id="S001", matric_number="MAT001",
                programme="Computer Science", level="100",
            )
            application.db.session.add_all([course, student])
            application.db.session.flush()
            application.db.session.add(application.Registration(
                student_id="S001", course_id="C001",
            ))
            application.db.session.commit()
            record_key = application._record_key(course, ("course_id",))
        response = self.client.post(
            f"/records/courses/{record_key}/delete", follow_redirects=True
        )
        self.assertIn(b"Delete dependent registrations", response.data)
        with self.web_app.app_context():
            self.assertIsNotNone(application.db.session.get(application.Course, "C001"))


if __name__ == "__main__":
    unittest.main()
