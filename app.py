from __future__ import annotations

import os
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import click
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import CheckConstraint, Index, text

db = SQLAlchemy()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
password_hasher = PasswordHasher()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def valid_email(value: str) -> bool:
    if len(value) > 254 or " " in value or value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    return bool(local and domain and "." in domain and not domain.startswith(".") and not domain.endswith("."))


class Organization(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("status in ('Pending','Approved','Suspended')"),)
    users = db.relationship("User", back_populates="organization")
    plans = db.relationship("Plan", back_populates="organization")


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(24), nullable=False)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    session_nonce = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("role in ('platform_admin','organization_admin','member')"),
        CheckConstraint("(role = 'platform_admin' and organization_id is null) or (role != 'platform_admin' and organization_id is not null)"),
        Index("uq_platform_admin_email", "email", unique=True,
              sqlite_where=text("role = 'platform_admin'"), postgresql_where=text("role = 'platform_admin'")),
        Index("uq_organization_account_email", "organization_id", "email", unique=True,
              sqlite_where=text("organization_id is not null"), postgresql_where=text("organization_id is not null")),
        Index("uq_one_organization_admin", "organization_id", unique=True,
              sqlite_where=text("role = 'organization_admin'"), postgresql_where=text("role = 'organization_admin'")),
    )
    organization = db.relationship("Organization", back_populates="users")


class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    price_minor = db.Column(db.Integer, nullable=False)
    billing_period = db.Column(db.String(12), nullable=False)
    benefits = db.Column(db.String(2000), nullable=False, default="")
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("price_minor >= 0"),
        CheckConstraint("billing_period in ('monthly','yearly')"),
    )
    organization = db.relationship("Organization", back_populates="plans")


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(80), nullable=False)
    object_type = db.Column(db.String(40), nullable=False)
    object_id = db.Column(db.Integer, nullable=False)
    details = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


def record_activity(action: str, object_type: str, object_id: int, details: str = "") -> None:
    db.session.add(ActivityLog(actor_user_id=getattr(g, "user", None).id if getattr(g, "user", None) else None,
                               action=action, object_type=object_type, object_id=object_id,
                               details=details[:500]))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///membership.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",  # Local HTTP only by default.
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        RATELIMIT_STORAGE_URI="memory://",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Set SECRET_KEY in the environment before starting the app.")

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    @app.before_request
    def load_user():
        g.user = db.session.get(User, session.get("user_id")) if session.get("user_id") else None
        if g.user and (not g.user.active or not g.user.session_nonce or not hmac.compare_digest(
                g.user.session_nonce, session.get("session_nonce", ""))):
            session.clear()
            g.user = None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        return response

    @app.get("/")
    def index():
        if not g.user:
            return render_template("index.html")
        if g.user.role == "platform_admin":
            return redirect(url_for("admin_organizations"))
        if g.user.role == "organization_admin":
            return redirect(url_for("organization_plans"))
        return render_template("index.html")

    @app.route("/organizations/register", methods=["GET", "POST"])
    def register_organization():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            org_email = request.form.get("organization_email", "").strip().lower()
            admin_email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or len(name) > 120 or not valid_email(org_email) or not valid_email(admin_email):
                flash("Enter a valid organization name and email addresses.", "error")
            elif len(password) < 12 or len(password) > 128:
                flash("Use a password between 12 and 128 characters.", "error")
            elif Organization.query.filter_by(email=org_email).first():
                flash("That organization contact email is already registered.", "error")
            else:
                organization = Organization(name=name, email=org_email)
                db.session.add(organization)
                db.session.flush()
                user = User(email=admin_email, password_hash=password_hasher.hash(password),
                            role="organization_admin", organization_id=organization.id)
                db.session.add(user)
                db.session.flush()
                record_activity("organization_registered", "organization", organization.id)
                db.session.commit()
                flash("Registration submitted. Sign in after platform approval.", "success")
                return redirect(url_for("login"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            organization_email = request.form.get("organization_email", "").strip().lower()
            candidates = User.query.filter_by(email=email).all()
            if organization_email:
                organization = Organization.query.filter_by(email=organization_email).first()
                user = next((u for u in candidates if organization and u.organization_id == organization.id), None)
            else:
                user = next((u for u in candidates if u.role == "platform_admin"), None)
                tenant_candidates = [u for u in candidates if u.organization_id is not None]
                if user is None and len(tenant_candidates) == 1:
                    user = tenant_candidates[0]
            password = request.form.get("password", "")
            valid = False
            if user and user.active:
                try:
                    valid = password_hasher.verify(user.password_hash, password)
                    if password_hasher.check_needs_rehash(user.password_hash):
                        user.password_hash = password_hasher.hash(password)
                        db.session.commit()
                except VerifyMismatchError:
                    pass
            if not valid:
                flash("Email or password is incorrect.", "error")
            elif user.organization_id is not None and user.organization.status != "Approved":
                flash("This organization is not currently approved for sign-in.", "error")
            else:
                session.clear()
                session.permanent = True
                user.session_nonce = secrets.token_urlsafe(32)
                db.session.commit()
                session["user_id"] = user.id
                session["session_nonce"] = user.session_nonce
                return redirect(url_for("index"))
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        g.user.session_nonce = secrets.token_urlsafe(32)
        db.session.commit()
        session.clear()
        return redirect(url_for("index"))

    @app.get("/admin/organizations")
    @role_required("platform_admin")
    def admin_organizations():
        organizations = Organization.query.order_by(Organization.created_at.desc()).all()
        return render_template("admin_organizations.html", organizations=organizations)

    @app.post("/admin/organizations/<int:organization_id>/<action>")
    @role_required("platform_admin")
    def admin_change_organization(organization_id: int, action: str):
        organization = db.session.get(Organization, organization_id)
        if not organization:
            abort(404)
        transitions = {"approve": "Approved", "suspend": "Suspended", "reinstate": "Approved"}
        new_status = transitions.get(action)
        if not new_status:
            abort(404)
        allowed_from = {"approve": "Pending", "suspend": "Approved", "reinstate": "Suspended"}
        if organization.status != allowed_from[action]:
            abort(409)
        old_status = organization.status
        organization.status = new_status
        record_activity(f"organization_{action}", "organization", organization.id,
                        f"{old_status} to {new_status}")
        db.session.commit()
        flash(f"{organization.name} is now {new_status.lower()}.", "success")
        return redirect(url_for("admin_organizations"))

    @app.route("/organization/plans", methods=["GET", "POST"])
    @role_required("organization_admin")
    def organization_plans():
        organization = db.session.get(Organization, g.user.organization_id)
        if request.method == "POST":
            if organization.status != "Approved":
                abort(403)
            name = request.form.get("name", "").strip()
            benefits = request.form.get("benefits", "").strip()
            period = request.form.get("billing_period", "")
            price = request.form.get("price_minor", "")
            if not name or len(name) > 100 or len(benefits) > 2000 or period not in ("monthly", "yearly"):
                flash("Enter a valid plan name, billing period, and benefits.", "error")
            else:
                try:
                    price_minor = int(price)
                    if price_minor < 0 or price_minor > 2_000_000_000:
                        raise ValueError
                except (TypeError, ValueError):
                    flash("Price must be a non-negative whole number of LKR minor units.", "error")
                else:
                    plan = Plan(organization_id=organization.id, name=name, benefits=benefits,
                                billing_period=period, price_minor=price_minor)
                    db.session.add(plan)
                    db.session.commit()
                    flash("Plan created.", "success")
                    return redirect(url_for("organization_plans"))
        plans = Plan.query.filter_by(organization_id=organization.id).order_by(Plan.id.desc()).all()
        return render_template("plans.html", organization=organization, plans=plans)

    @app.post("/organization/plans/<int:plan_id>/archive")
    @role_required("organization_admin")
    def archive_plan(plan_id: int):
        plan = Plan.query.filter_by(id=plan_id, organization_id=g.user.organization_id).first()
        if not plan:
            abort(404)
        plan.archived = True
        db.session.commit()
        flash("Plan archived.", "success")
        return redirect(url_for("organization_plans"))

    @app.cli.command("create-admin")
    def create_admin():
        """Create the first platform administrator interactively."""
        email = click.prompt("Platform administrator email").strip().lower()
        password = click.prompt("Password (12+ characters)", hide_input=True, confirmation_prompt=True)
        if not valid_email(email) or len(password) < 12 or len(password) > 128 or User.query.filter_by(email=email, role="platform_admin").first():
            raise click.ClickException("Password length is invalid or that email already exists.")
        db.session.add(User(email=email, password_hash=password_hasher.hash(password), role="platform_admin"))
        db.session.commit()
        click.echo("Platform administrator created.")

    @app.cli.command("init-db")
    def init_db():
        """Create local tables for the initial prototype."""
        db.create_all()
        click.echo("Database tables created. Use migrations for later schema changes." )

    return app
