from datetime import date, datetime, timedelta, time
import math
from functools import wraps
from pathlib import Path
import re
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, select, text
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.config.update(
    SECRET_KEY="change-this-secret-key-in-production",
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{BASE_DIR / 'library.db'}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)
db = SQLAlchemy(app)


@app.context_processor
def inject_current_year():
    return {"current_year": datetime.now().year}


MAX_BOOKS_PER_READER = 4
LOAN_PERIOD_DAYS = 14
LATE_FEE_PER_DAY = 10


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="reader")
    approval_status = db.Column(db.String(20), nullable=False, default="approved")
    active = db.Column(db.Boolean, nullable=False, default=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    loans = db.relationship("Loan", back_populates="reader")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Book(db.Model):
    __tablename__ = "books"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    author = db.Column(db.String(150), nullable=False)
    isbn = db.Column(db.String(30), unique=True, nullable=False)
    category = db.Column(db.String(100), nullable=False, default="General")
    total_copies = db.Column(db.Integer, nullable=False, default=1)
    available_copies = db.Column(db.Integer, nullable=False, default=1)
    price = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    loans = db.relationship("Loan", back_populates="book")


class Loan(db.Model):
    __tablename__ = "loans"
    id = db.Column(db.Integer, primary_key=True)
    reader_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id"), nullable=False)
    issued_at = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(
        db.Date, nullable=False
    )  # legacy date field kept for old records
    due_at = db.Column(db.DateTime, nullable=True)
    returned_at = db.Column(
        db.Date, nullable=True
    )  # legacy date field kept for old records
    returned_at_exact = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="issued")
    penalty = db.Column(db.Float, nullable=False, default=0.0)
    reader = db.relationship("User", back_populates="loans")
    book = db.relationship("Book", back_populates="loans")

    @property
    def due_datetime(self):
        return self.due_at or datetime.combine(self.due_date, time.min)

    @property
    def returned_datetime(self):
        if self.returned_at_exact:
            return self.returned_at_exact
        if self.returned_at:
            return datetime.combine(self.returned_at, time.min)
        return None

    @property
    def is_overdue(self):
        return self.status == "issued" and datetime.now() > self.due_datetime

    @property
    def late_days(self):
        if self.status == "returned":
            if (
                not self.returned_datetime
                or self.returned_datetime <= self.due_datetime
            ):
                return 0
            seconds_late = (self.returned_datetime - self.due_datetime).total_seconds()
        else:
            now = datetime.now()
            if now <= self.due_datetime:
                return 0
            seconds_late = (now - self.due_datetime).total_seconds()
        return max(0, math.ceil(seconds_late / 86400))

    @property
    def remaining_seconds(self):
        return max(0, int((self.due_datetime - datetime.now()).total_seconds()))

    @property
    def current_penalty(self):
        if self.status == "returned":
            return self.penalty
        return min(self.late_days * LATE_FEE_PER_DAY, self.book.price)


# ---------- Helpers ----------
def today_date():
    return date.today()


def now_datetime():
    return datetime.now()


def max_return_datetime():
    return datetime.now() + timedelta(days=LOAN_PERIOD_DAYS)


def datetime_local_value(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def valid_email(email):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+**\\.**[^\s@]+", email))


def current_user():
    user_id = session.get("user_id")
    user = db.session.get(User, user_id) if user_id else None
    if user and not user.active:
        session.clear()
        return None
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def role_required(role):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or user.role != role:
                flash("You do not have permission to access that page.", "danger")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def parse_return_datetime(raw_value):
    """Accept an exact local date/time from now through exactly 14 days from now."""
    try:
        selected_time = datetime.strptime(raw_value, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        try:
            selected_time = datetime.strptime(raw_value, "%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            return None
    current_time = datetime.now()
    latest_allowed = current_time + timedelta(days=LOAN_PERIOD_DAYS)
    if selected_time < current_time or selected_time > latest_allowed:
        return None
    return selected_time


def calculate_penalty(loan, returned_on=None):
    returned_on = returned_on or datetime.now()
    if returned_on <= loan.due_datetime:
        return 0.0
    late_days = math.ceil((returned_on - loan.due_datetime).total_seconds() / 86400)
    return min(late_days * LATE_FEE_PER_DAY, loan.book.price)


def finish_return(loan, returned_on=None):
    returned_on = returned_on or datetime.now()
    loan.status = "returned"
    loan.returned_at_exact = returned_on
    loan.returned_at = returned_on.date()
    loan.penalty = calculate_penalty(loan, returned_on)
    loan.book.available_copies = min(
        loan.book.total_copies, loan.book.available_copies + 1
    )


def active_loan_count(reader_id):
    return (
        db.session.scalar(
            select(db.func.count(Loan.id)).where(
                Loan.reader_id == reader_id, Loan.status == "issued"
            )
        )
        or 0
    )


def reader_can_borrow(reader, book):
    if not reader.active or reader.approval_status != "approved":
        return False, "This reader account is not active."
    if book.available_copies < 1:
        return False, "This book is currently unavailable."
    if active_loan_count(reader.id) >= MAX_BOOKS_PER_READER:
        return (
            False,
            f"A reader can have a maximum of {MAX_BOOKS_PER_READER} active books.",
        )
    existing_loan = db.session.scalar(
        select(Loan).where(
            Loan.reader_id == reader.id,
            Loan.book_id == book.id,
            Loan.status == "issued",
        )
    )
    if existing_loan:
        return False, "This reader already has this book."
    return True, ""


# ---------- Database setup / migration ----------
def prepare_database():
    with app.app_context():
        db.create_all()
        inspector = inspect(db.engine)
        table_columns = {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in ("users", "books", "loans")
            if inspector.has_table(table)
        }
        migrations = {
            "users": {
                "approval_status": "VARCHAR(20) NOT NULL DEFAULT 'approved'",
                "approved_at": "DATETIME",
                "rejection_reason": "VARCHAR(500)",
                "active": "BOOLEAN NOT NULL DEFAULT 1",
            },
            "books": {
                "category": "VARCHAR(100) NOT NULL DEFAULT 'General'",
                "price": "FLOAT NOT NULL DEFAULT 0",
            },
            "loans": {
                "penalty": "FLOAT NOT NULL DEFAULT 0",
                "due_at": "DATETIME",
                "returned_at_exact": "DATETIME",
            },
        }
        with db.engine.begin() as connection:
            for table_name, columns in migrations.items():
                existing = table_columns.get(table_name, set())
                for column_name, definition in columns.items():
                    if column_name not in existing:
                        connection.execute(
                            text(
                                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
                            )
                        )


def seed_database():
    with app.app_context():
        librarian = db.session.scalar(
            select(User).where(User.email == "librarian@library.local")
        )
        if not librarian:
            librarian = User(
                name="Main Librarian",
                email="librarian@library.local",
                role="librarian",
                approval_status="approved",
                active=True,
                approved_at=datetime.utcnow(),
            )
            librarian.set_password("Librarian@123")
            db.session.add(librarian)
        demo_reader = db.session.scalar(
            select(User).where(User.email == "student@library.local")
        )
        if not demo_reader:
            demo_reader = User(
                name="Demo Student",
                email="student@library.local",
                role="reader",
                approval_status="approved",
                active=True,
                approved_at=datetime.utcnow(),
            )
            demo_reader.set_password("Student@123")
            db.session.add(demo_reader)
        if db.session.scalar(select(Book).limit(1)) is None:
            db.session.add_all(
                [
                    Book(
                        title="Python Crash Course",
                        author="Eric Matthes",
                        isbn="9781593279288",
                        category="Programming",
                        total_copies=3,
                        available_copies=3,
                        price=650,
                    ),
                    Book(
                        title="Clean Code",
                        author="Robert C. Martin",
                        isbn="9780132350884",
                        category="Software Engineering",
                        total_copies=2,
                        available_copies=2,
                        price=850,
                    ),
                    Book(
                        title="The Web Application Hacker's Handbook",
                        author="Dafydd Stuttard",
                        isbn="9781118026472",
                        category="Cybersecurity",
                        total_copies=2,
                        available_copies=2,
                        price=1200,
                    ),
                    Book(
                        title="Database System Concepts",
                        author="Abraham Silberschatz",
                        isbn="9780078022159",
                        category="Database",
                        total_copies=2,
                        available_copies=2,
                        price=900,
                    ),
                    Book(
                        title="Introduction to Algorithms",
                        author="Thomas H. Cormen",
                        isbn="9780262046305",
                        category="Algorithms",
                        total_copies=1,
                        available_copies=1,
                        price=1500,
                    ),
                ]
            )
        db.session.commit()


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "today": date.today(),
        "now": now_datetime(),
        "max_return_datetime": max_return_datetime(),
        "datetime_local_value": datetime_local_value,
        "max_books_per_reader": MAX_BOOKS_PER_READER,
        "loan_period_days": LOAN_PERIOD_DAYS,
        "late_fee_per_day": LATE_FEE_PER_DAY,
    }


# ---------- Authentication ----------
@app.route("/", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.scalar(select(User).where(User.email == email))
        if not user or not user.check_password(password):
            flash("Invalid email or password.", "danger")
        elif not user.active:
            flash("This account has been removed by the librarian.", "danger")
        elif user.approval_status == "pending":
            flash("Your account is waiting for librarian approval.", "warning")
        elif user.approval_status == "rejected":
            reason = (
                f" Reason: {user.rejection_reason}" if user.rejection_reason else ""
            )
            flash(f"Your registration was rejected.{reason}", "danger")
        else:
            session.clear()
            session["user_id"] = user.id
            flash(f"Welcome, {user.name}!", "success")
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not name or not email or not password:
            flash("Please fill in all required fields.", "danger")
        elif not valid_email(email):
            flash("Please enter a valid email address.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        elif len(password) < 8:
            flash("Password must contain at least 8 characters.", "danger")
        elif db.session.scalar(select(User).where(User.email == email)):
            flash("An account with this email already exists.", "warning")
        else:
            reader = User(
                name=name,
                email=email,
                role="reader",
                approval_status="pending",
                active=True,
            )
            reader.set_password(password)
            db.session.add(reader)
            db.session.commit()
            flash(
                "Registration submitted. Please wait for librarian approval before logging in.",
                "success",
            )
            return redirect(url_for("login"))
    return render_template("register.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    user = current_user()
    return redirect(
        url_for(
            "librarian_dashboard" if user.role == "librarian" else "reader_dashboard"
        )
    )


# ---------- Profile / password ----------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        if not name or not valid_email(email):
            flash("Enter a valid name and email address.", "danger")
        else:
            existing = db.session.scalar(
                select(User).where(User.email == email, User.id != user.id)
            )
            if existing:
                flash("That email address is already in use.", "danger")
            else:
                user.name = name
                user.email = email
                db.session.commit()
                flash("Profile updated successfully.", "success")
                return redirect(url_for("profile"))
    return render_template("profile.html")


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not user.check_password(current_password):
            flash("Current password is incorrect.", "danger")
        elif len(new_password) < 8:
            flash("New password must contain at least 8 characters.", "danger")
        elif new_password != confirm_password:
            flash("New passwords do not match.", "danger")
        else:
            user.set_password(new_password)
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("profile"))
    return render_template("change_password.html")


# ---------- Librarian ----------
@app.get("/librarian")
@role_required("librarian")
def librarian_dashboard():
    books = db.session.scalars(select(Book).order_by(Book.title)).all()
    loans = db.session.scalars(
        select(Loan).order_by(Loan.issued_at.desc(), Loan.id.desc())
    ).all()
    readers = db.session.scalars(
        select(User).where(User.role == "reader").order_by(User.name)
    ).all()
    pending_readers = [
        reader
        for reader in readers
        if reader.approval_status == "pending" and reader.active
    ]
    active_readers = [
        reader
        for reader in readers
        if reader.approval_status == "approved" and reader.active
    ]
    stats = {
        "titles": len(books),
        "copies": sum(book.total_copies for book in books),
        "available": sum(book.available_copies for book in books),
        "readers": len(active_readers),
        "pending": len(pending_readers),
        "active_loans": sum(loan.status == "issued" for loan in loans),
        "overdue": sum(loan.is_overdue for loan in loans),
        "penalties": sum(
            loan.current_penalty for loan in loans if loan.status == "issued"
        )
        + sum(loan.penalty for loan in loans if loan.status == "returned"),
    }
    return render_template(
        "librarian.html",
        books=books,
        loans=loans,
        readers=readers,
        pending_readers=pending_readers,
        stats=stats,
    )


@app.post("/librarian/readers/<int:reader_id>/approve")
@role_required("librarian")
def approve_reader(reader_id):
    reader = db.session.get(User, reader_id)
    if not reader or reader.role != "reader" or not reader.active:
        flash("Reader not found.", "danger")
        return redirect(url_for("librarian_dashboard"))
    reader.approval_status = "approved"
    reader.approved_at = datetime.utcnow()
    reader.rejection_reason = None
    db.session.commit()
    flash(f"{reader.name}'s registration has been approved.", "success")
    return redirect(url_for("librarian_dashboard"))


@app.post("/librarian/readers/<int:reader_id>/reject")
@role_required("librarian")
def reject_reader(reader_id):
    reader = db.session.get(User, reader_id)
    if not reader or reader.role != "reader" or not reader.active:
        flash("Reader not found.", "danger")
        return redirect(url_for("librarian_dashboard"))
    reason = request.form.get("reason", "Registration was not approved.").strip()[:500]
    reader.approval_status = "rejected"
    reader.rejection_reason = reason or "Registration was not approved."
    reader.approved_at = None
    db.session.commit()
    flash(f"{reader.name}'s registration has been rejected.", "warning")
    return redirect(url_for("librarian_dashboard"))


@app.post("/librarian/readers/<int:reader_id>/remove")
@role_required("librarian")
def remove_reader(reader_id):
    reader = db.session.get(User, reader_id)
    if not reader or reader.role != "reader":
        flash("Reader not found.", "danger")
        return redirect(url_for("librarian_dashboard"))
    if active_loan_count(reader.id) > 0:
        flash(
            "This reader still has active books. Return all books before removing the account.",
            "danger",
        )
        return redirect(url_for("librarian_dashboard"))
    reader.active = False
    reader.approval_status = "rejected"
    db.session.commit()
    flash(f"{reader.name}'s account has been removed.", "success")
    return redirect(url_for("librarian_dashboard"))


@app.post("/librarian/books/add")
@role_required("librarian")
def add_book():
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    isbn = request.form.get("isbn", "").strip()
    category = request.form.get("category", "General").strip() or "General"
    try:
        copies = int(request.form.get("copies", "1"))
        price = float(request.form.get("price", "0"))
    except ValueError:
        copies, price = 0, -1
    if not title or not author or not isbn or copies < 1 or price < 0:
        flash(
            "Enter valid book details, at least one copy and a non-negative price.",
            "danger",
        )
    elif db.session.scalar(select(Book).where(Book.isbn == isbn)):
        flash("A book with that ISBN already exists.", "danger")
    else:
        db.session.add(
            Book(
                title=title,
                author=author,
                isbn=isbn,
                category=category,
                total_copies=copies,
                available_copies=copies,
                price=price,
            )
        )
        db.session.commit()
        flash("Book added successfully.", "success")
    return redirect(url_for("librarian_dashboard"))


@app.route("/librarian/books/<int:book_id>/edit", methods=["GET", "POST"])
@role_required("librarian")
def edit_book(book_id):
    book = db.session.get(Book, book_id)
    if not book:
        flash("Book not found.", "danger")
        return redirect(url_for("librarian_dashboard"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        isbn = request.form.get("isbn", "").strip()
        category = request.form.get("category", "General").strip() or "General"
        try:
            new_total = int(request.form.get("copies", "1"))
            price = float(request.form.get("price", "0"))
        except ValueError:
            new_total, price = 0, -1
        active_issues = sum(loan.status == "issued" for loan in book.loans)
        if (
            not title
            or not author
            or not isbn
            or new_total < active_issues
            or price < 0
        ):
            flash(
                f"Copies cannot be lower than currently issued copies ({active_issues}).",
                "danger",
            )
        elif db.session.scalar(
            select(Book).where(Book.isbn == isbn, Book.id != book.id)
        ):
            flash("Another book already uses that ISBN.", "danger")
        else:
            book.title = title
            book.author = author
            book.isbn = isbn
            book.category = category
            book.price = price
            book.total_copies = new_total
            book.available_copies = new_total - active_issues
            db.session.commit()
            flash("Book details updated.", "success")
            return redirect(url_for("librarian_dashboard"))
    return render_template("book_form.html", book=book)


@app.post("/librarian/books/<int:book_id>/delete")
@role_required("librarian")
def delete_book(book_id):
    book = db.session.get(Book, book_id)
    if not book:
        flash("Book not found.", "danger")
    elif any(loan.status == "issued" for loan in book.loans):
        flash(
            "Cannot delete a book while it is issued. Return all copies first.",
            "danger",
        )
    else:
        # Keep the system simple while preserving loan history by deleting the book only when it has no history.
        if book.loans:
            flash(
                "This book has loan history and cannot be permanently deleted. Edit it or keep it for records.",
                "warning",
            )
        else:
            db.session.delete(book)
            db.session.commit()
            flash("Book deleted.", "success")
    return redirect(url_for("librarian_dashboard"))


@app.post("/librarian/issue")
@role_required("librarian")
def issue_book_by_librarian():
    try:
        reader_id = int(request.form.get("reader_id", "0"))
        book_id = int(request.form.get("book_id", "0"))
    except ValueError:
        reader_id = book_id = 0
    due_at = parse_return_datetime(request.form.get("return_at", ""))
    reader = db.session.get(User, reader_id)
    book = db.session.get(Book, book_id)
    if (
        not reader
        or reader.role != "reader"
        or reader.approval_status != "approved"
        or not reader.active
        or not book
    ):
        flash("Select a valid active approved reader and book.", "danger")
        return redirect(url_for("librarian_dashboard"))
    if not due_at:
        flash(
            "Return time must be from now through exactly 14 days from now. Past times and anything even one second beyond the limit are not allowed.",
            "danger",
        )
        return redirect(url_for("librarian_dashboard"))
    allowed, message = reader_can_borrow(reader, book)
    if not allowed:
        flash(message, "warning")
        return redirect(url_for("librarian_dashboard"))
    loan = Loan(
        reader_id=reader.id, book_id=book.id, due_date=due_at.date(), due_at=due_at
    )
    book.available_copies -= 1
    db.session.add(loan)
    db.session.commit()
    flash(
        f"{book.title} issued to {reader.name} until {due_at.strftime('%d %b %Y at %I:%M %p')}.",
        "success",
    )
    return redirect(url_for("librarian_dashboard"))


@app.post("/librarian/loans/<int:loan_id>/return")
@role_required("librarian")
def librarian_return(loan_id):
    loan = db.session.get(Loan, loan_id)
    if not loan or loan.status != "issued":
        flash("Active loan not found.", "danger")
    else:
        finish_return(loan)
        db.session.commit()
        if loan.penalty:
            flash(f"Book returned. Late penalty: ₹{loan.penalty:.0f}.", "warning")
        else:
            flash("Book returned successfully.", "success")
    return redirect(url_for("librarian_dashboard"))


# ---------- Reader ----------
@app.get("/reader")
@role_required("reader")
def reader_dashboard():
    user = current_user()
    books = db.session.scalars(select(Book).order_by(Book.title)).all()
    loans = db.session.scalars(
        select(Loan)
        .where(Loan.reader_id == user.id)
        .order_by(Loan.issued_at.desc(), Loan.id.desc())
    ).all()
    active_loans = [loan for loan in loans if loan.status == "issued"]
    return render_template(
        "reader.html", books=books, loans=loans, active_loans=active_loans
    )


@app.post("/reader/borrow/<int:book_id>")
@role_required("reader")
def borrow_book(book_id):
    user = current_user()
    book = db.session.get(Book, book_id)
    if not book:
        flash("Book not found.", "danger")
        return redirect(url_for("reader_dashboard"))
    due_at = parse_return_datetime(request.form.get("return_at", ""))
    if not due_at:
        flash(
            "Choose a return date and time from now through exactly 14 days from now. Past times and later times are not allowed.",
            "danger",
        )
        return redirect(url_for("reader_dashboard"))
    allowed, message = reader_can_borrow(user, book)
    if not allowed:
        flash(message, "warning")
        return redirect(url_for("reader_dashboard"))
    loan = Loan(
        reader_id=user.id, book_id=book.id, due_date=due_at.date(), due_at=due_at
    )
    book.available_copies -= 1
    db.session.add(loan)
    db.session.commit()
    flash(
        f"You borrowed {book.title}. Return it by {due_at.strftime('%d %b %Y at %I:%M %p')}.",
        "success",
    )
    return redirect(url_for("reader_dashboard"))


@app.post("/reader/return/<int:loan_id>")
@role_required("reader")
def reader_return(loan_id):
    user = current_user()
    loan = db.session.get(Loan, loan_id)
    if not loan or loan.reader_id != user.id or loan.status != "issued":
        flash("Active loan not found.", "danger")
    else:
        finish_return(loan)
        db.session.commit()
        if loan.penalty:
            flash(
                f"Book returned. Your late penalty is ₹{loan.penalty:.0f}.", "warning"
            )
        else:
            flash("Book returned successfully.", "success")
    return redirect(url_for("reader_dashboard"))


with app.app_context():
    prepare_database()
    seed_database()
if __name__ == "__main__":
    app.run(debug=True)
