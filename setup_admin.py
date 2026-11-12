from __future__ import annotations

import getpass
import os
import re


def prompt_value(label, default):
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def main():
    if (
        not os.environ.get("DATABASE_URL")
        and os.environ.get("MYSQL_DATABASE")
        and not os.environ.get("MYSQL_PASSWORD")
    ):
        os.environ["MYSQL_PASSWORD"] = getpass.getpass("MySQL password: ")

    email = input("Administrator email [marcellenusajigiri@gail.com]: ").strip().casefold()
    email = email or "marcellenusajigiri@gail.com"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise SystemExit("Enter a valid administrator email.")

    name = prompt_value("Administrator name", "Examinations Administrator")
    password = getpass.getpass("New admin password (12+ characters): ")
    confirmation = getpass.getpass("Confirm admin password: ")
    if len(password) < 12 or len(password) > 128:
        raise SystemExit("Admin password must contain 12 to 128 characters.")
    if password != confirmation:
        raise SystemExit("Passwords did not match; no account was changed.")

    import app as scheduling_app

    with scheduling_app.app.app_context():
        account = scheduling_app.ExaminerAccount.query.filter_by(email=email).first()
        if account is None:
            account = scheduling_app.ExaminerAccount(email=email)
            scheduling_app.db.session.add(account)
        account.full_name = name
        account.department = "Examinations Administration"
        account.password_hash = scheduling_app.generate_password_hash(password)
        account.is_admin = True
        account.is_approved = True
        scheduling_app.db.session.commit()

    print(f"Administrator account is ready for {email}.")
    print("You can now sign in at http://127.0.0.1:5000/login.")


if __name__ == "__main__":
    main()
