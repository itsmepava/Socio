from __future__ import annotations

import os
import hmac
import secrets
import csv
import io
import hashlib
import smtplib
from email.message import EmailMessage
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlsplit

import click
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from PIL import Image, ImageOps, UnidentifiedImageError

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


def normalize_uploaded_image(raw: bytes, max_side: int, max_output_bytes: int) -> bytes:
    """Decode and re-encode supported raster images, discarding source metadata."""
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in ("PNG", "JPEG", "WEBP"):
            raise ValueError("Choose a PNG, JPEG, or WebP image.")
        if source.width < 1 or source.height < 1 or source.width * source.height > 20_000_000:
            raise ValueError("The image dimensions are too large. Choose an image under 20 megapixels.")
        source.verify()
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        normalized = io.BytesIO()
        image.save(normalized, format="PNG", optimize=True)
    result = normalized.getvalue()
    if len(result) > max_output_bytes:
        raise ValueError("The processed image is still too large. Choose a simpler or smaller image.")
    return result


def spreadsheet_safe(value: object) -> str:
    text_value = "" if value is None else str(value)
    if text_value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text_value
    return text_value


class Organization(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    membership_approval_required = db.Column(db.Boolean, nullable=False, default=True, server_default=text("1"))
    recommendation_required = db.Column(db.Boolean, nullable=False, default=False, server_default=text("0"))
    application_prompt = db.Column(db.String(500), nullable=False, default="", server_default="")
    logo_filename = db.Column(db.String(80), nullable=True)
    loyalty_reward_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=text("0"))
    loyalty_reward_after_days = db.Column(db.Integer, nullable=False, default=30, server_default=text("30"))
    loyalty_reward_days = db.Column(db.Integer, nullable=False, default=30, server_default=text("30"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("status in ('Pending','Approved','Suspended')"),)
    users = db.relationship("User", back_populates="organization")
    plans = db.relationship("Plan", back_populates="organization")
    memberships = db.relationship("Membership", back_populates="organization")
    offers = db.relationship("OrganizationOffer", back_populates="organization")
    events = db.relationship("OrganizationEvent", back_populates="organization")


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=True)
    gender = db.Column(db.String(40), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(24), nullable=False)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    email_verified = db.Column(db.Boolean, nullable=False, default=True)
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
    offers = db.relationship("OrganizationOffer", back_populates="plan")


class OrganizationOffer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True, index=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    offer_type = db.Column(db.String(24), nullable=False, default="Extra benefit")
    start_day = db.Column(db.Integer, nullable=False, default=0)
    end_day = db.Column(db.Integer, nullable=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("offer_type in ('Welcome bonus','Extra benefit','Limited time','Other')"),
        CheckConstraint("start_day >= 0"),
        CheckConstraint("end_day is null or end_day >= start_day"),
    )
    organization = db.relationship("Organization", back_populates="offers")
    plan = db.relationship("Plan", back_populates="offers")


class OrganizationEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(1000), nullable=False)
    venue = db.Column(db.String(200), nullable=False, default="")
    event_date = db.Column(db.Date, nullable=False, index=True)
    poster_filename = db.Column(db.String(80), nullable=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    organization = db.relationship("Organization", back_populates="events")


class Membership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    start_day = db.Column(db.Integer, nullable=True)
    end_day = db.Column(db.Integer, nullable=True)
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    next_plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True)
    last_renewal_due_day = db.Column(db.Integer, nullable=True)
    loyalty_rewarded_day = db.Column(db.Integer, nullable=True)
    loyalty_reward_days = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_organization_user"),
        CheckConstraint("status in ('Pending','Active')"),
        CheckConstraint("start_day is null or start_day >= 0"),
        CheckConstraint("end_day is null or end_day >= 0"),
    )
    organization = db.relationship("Organization", back_populates="memberships")
    user = db.relationship("User")
    plan = db.relationship("Plan", foreign_keys=[plan_id])
    next_plan = db.relationship("Plan", foreign_keys=[next_plan_id])
    payments = db.relationship("SimulatedPayment", back_populates="membership", order_by="SimulatedPayment.id.desc()")


class MembershipApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    recommendation_text = db.Column(db.Text, nullable=False, default="")
    review_note = db.Column(db.String(500), nullable=False, default="")
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    __table_args__ = (CheckConstraint("status in ('Pending','Approved','Declined')"),)
    membership = db.relationship("Membership")
    reviewed_by = db.relationship("User")


class SimulatedPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    membership_id = db.Column(db.Integer, db.ForeignKey("membership.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    amount_minor = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    channel = db.Column(db.String(12), nullable=False, default="simulated")
    status = db.Column(db.String(16), nullable=False, default="Pending")
    created_day = db.Column(db.Integer, nullable=False)
    confirmed_day = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("amount_minor >= 0"),
        CheckConstraint("kind in ('join','upgrade','renewal')"),
        CheckConstraint("status in ('Pending','Paid','Failed','Abandoned')"),
        CheckConstraint("channel in ('simulated','offline')"),
    )
    organization = db.relationship("Organization")
    membership = db.relationship("Membership", back_populates="payments")
    plan = db.relationship("Plan")
    refund_request = db.relationship("SimulatedRefund", back_populates="payment", uselist=False)


class SimulationState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    current_day = db.Column(db.Integer, nullable=False, default=0)


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(80), nullable=False)
    object_type = db.Column(db.String(40), nullable=False)
    object_id = db.Column(db.Integer, nullable=False)
    details = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class SimulatedRefund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("simulated_payment.id"), nullable=False, unique=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    amount_minor = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="Requested")
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    processed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_day = db.Column(db.Integer, nullable=False)
    processed_day = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("status in ('Requested','Approved','Declined')"),
    )
    payment = db.relationship("SimulatedPayment", back_populates="refund_request")
    organization = db.relationship("Organization")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])
    processed_by = db.relationship("User", foreign_keys=[processed_by_user_id])


class MemberInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    status = db.Column(db.String(16), nullable=False, default="Pending")
    created_day = db.Column(db.Integer, nullable=False)
    expires_day = db.Column(db.Integer, nullable=False)
    claimed_day = db.Column(db.Integer, nullable=True)
    claimed_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (CheckConstraint("status in ('Pending','Claimed','Revoked')"),)
    organization = db.relationship("Organization")
    plan = db.relationship("Plan")
    claimed_user = db.relationship("User", foreign_keys=[claimed_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])


class EmailVerificationToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    user = db.relationship("User")


def record_activity(action: str, object_type: str, object_id: int, details: str = "") -> None:
    db.session.add(ActivityLog(actor_user_id=getattr(g, "user", None).id if getattr(g, "user", None) else None,
                               action=action, object_type=object_type, object_id=object_id,
                               details=details[:500]))


def simulated_day() -> int:
    state = db.session.get(SimulationState, 1)
    if not state:
        state = SimulationState(id=1, current_day=0)
        db.session.add(state)
        db.session.commit()
    return state.current_day


def period_days(plan: Plan) -> int:
    return 365 if plan.billing_period == "yearly" else 30


def membership_status(membership: Membership) -> str:
    if membership.status == "Pending":
        return "Pending"
    if membership.end_day is not None and simulated_day() >= membership.end_day:
        return "Expired"
    if membership.cancel_at_period_end:
        return "Cancelled"
    return "Active"


def grant_loyalty_rewards(organization: Organization, previous_day: int, current_day: int,
                          include_already_qualified: bool = False) -> int:
    """Give each continuously active member one configured, no-charge membership extension."""
    if not organization.loyalty_reward_enabled:
        return 0
    eligible = Membership.query.filter_by(organization_id=organization.id, status="Active").filter(
        Membership.cancel_at_period_end.is_(False),
        Membership.start_day.is_not(None), Membership.end_day.is_not(None),
        Membership.loyalty_rewarded_day.is_(None),
    ).all()
    rewarded = 0
    for membership in eligible:
        threshold_day = membership.start_day + organization.loyalty_reward_after_days
        reached_during_advance = previous_day < threshold_day <= current_day
        already_active_and_qualified = (include_already_qualified and threshold_day <= current_day
                                        and membership.end_day > current_day)
        if ((reached_during_advance and membership.end_day >= threshold_day)
                or already_active_and_qualified):
            membership.end_day += organization.loyalty_reward_days
            membership.loyalty_rewarded_day = current_day
            membership.loyalty_reward_days = organization.loyalty_reward_days
            record_activity("loyalty_membership_days_gifted", "membership", membership.id,
                            f"{organization.loyalty_reward_days} no-charge days after {organization.loyalty_reward_after_days} active days")
            rewarded += 1
    return rewarded


def current_organization_offers(organization_id: int) -> list[OrganizationOffer]:
    today = simulated_day()
    return OrganizationOffer.query.filter_by(organization_id=organization_id, archived=False).filter(
        OrganizationOffer.start_day <= today,
        or_(OrganizationOffer.end_day.is_(None), OrganizationOffer.end_day >= today)
    ).order_by(OrganizationOffer.id.desc()).all()


def establish_session(user: User) -> None:
    session.clear()
    session.permanent = True
    user.session_nonce = secrets.token_urlsafe(32)
    db.session.commit()
    session["user_id"] = user.id
    session["session_nonce"] = user.session_nonce


def ensure_local_sqlite_schema() -> None:
    """Create demo tables and add non-destructive local billing columns."""
    if db.engine.dialect.name != "sqlite":
        return
    db.create_all()
    organization_columns = {column["name"] for column in inspect(db.engine).get_columns("organization")}
    with db.engine.begin() as connection:
        if "membership_approval_required" not in organization_columns:
            # Existing demo organizations keep their prior open-join behavior;
            # newly registered organizations use the model's approval-required default.
            connection.execute(text("ALTER TABLE organization ADD COLUMN membership_approval_required BOOLEAN NOT NULL DEFAULT 0"))
        if "recommendation_required" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN recommendation_required BOOLEAN NOT NULL DEFAULT 0"))
        if "application_prompt" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN application_prompt VARCHAR(500) NOT NULL DEFAULT ''"))
        if "logo_filename" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN logo_filename VARCHAR(80)"))
        if "loyalty_reward_enabled" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN loyalty_reward_enabled BOOLEAN NOT NULL DEFAULT 0"))
        if "loyalty_reward_after_days" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN loyalty_reward_after_days INTEGER NOT NULL DEFAULT 30"))
        if "loyalty_reward_days" not in organization_columns:
            connection.execute(text("ALTER TABLE organization ADD COLUMN loyalty_reward_days INTEGER NOT NULL DEFAULT 30"))
    user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
    with db.engine.begin() as connection:
        if "full_name" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN full_name VARCHAR(120)"))
        if "gender" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN gender VARCHAR(40)"))
        if "email_verified" not in user_columns:
            # Existing local demo accounts predate confirmation and remain usable.
            connection.execute(text("ALTER TABLE user ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT 1"))
    columns = {column["name"] for column in inspect(db.engine).get_columns("membership")}
    with db.engine.begin() as connection:
        if "next_plan_id" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN next_plan_id INTEGER REFERENCES plan(id)"))
        if "last_renewal_due_day" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN last_renewal_due_day INTEGER"))
        if "loyalty_rewarded_day" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN loyalty_rewarded_day INTEGER"))
        if "loyalty_reward_days" not in columns:
            connection.execute(text("ALTER TABLE membership ADD COLUMN loyalty_reward_days INTEGER NOT NULL DEFAULT 0"))
    payment_columns = {column["name"] for column in inspect(db.engine).get_columns("simulated_payment")}
    if "channel" not in payment_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE simulated_payment ADD COLUMN channel VARCHAR(12) NOT NULL DEFAULT 'simulated'"))
    membership_uniques = {constraint.get("name") for constraint in inspect(db.engine).get_unique_constraints("membership")}
    if "uq_membership_organization_user" not in membership_uniques:
        # Older demos enforced one membership per member account. Rebuild only
        # this table; dependent payment rows keep referencing the same table name.
        raw = db.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("""CREATE TABLE membership_new (
                id INTEGER NOT NULL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organization(id),
                user_id INTEGER NOT NULL REFERENCES user(id),
                plan_id INTEGER NOT NULL REFERENCES plan(id),
                full_name VARCHAR(120) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'Pending',
                start_day INTEGER,
                end_day INTEGER,
                cancel_at_period_end BOOLEAN NOT NULL DEFAULT 0,
                next_plan_id INTEGER REFERENCES plan(id),
                last_renewal_due_day INTEGER,
                loyalty_rewarded_day INTEGER,
                loyalty_reward_days INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_membership_organization_user UNIQUE (organization_id, user_id),
                CONSTRAINT ck_membership_status CHECK (status in ('Pending','Active')),
                CONSTRAINT ck_membership_start_day CHECK (start_day is null or start_day >= 0),
                CONSTRAINT ck_membership_end_day CHECK (end_day is null or end_day >= 0)
            )""")
            cursor.execute("""INSERT INTO membership_new
                (id, organization_id, user_id, plan_id, full_name, status, start_day, end_day,
                 cancel_at_period_end, next_plan_id, last_renewal_due_day, loyalty_rewarded_day,
                 loyalty_reward_days, created_at)
                SELECT id, organization_id, user_id, plan_id, full_name, status, start_day, end_day,
                   cancel_at_period_end, next_plan_id, last_renewal_due_day, loyalty_rewarded_day,
                   loyalty_reward_days, created_at FROM membership""")
            cursor.execute("DROP TABLE membership")
            cursor.execute("ALTER TABLE membership_new RENAME TO membership")
            cursor.execute("CREATE INDEX ix_membership_organization_id ON membership (organization_id)")
            cursor.execute("CREATE INDEX ix_membership_user_id ON membership (user_id)")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()


def create_simulated_payment(membership: Membership, plan: Plan, kind: str, amount_minor: int) -> SimulatedPayment:
    pending = SimulatedPayment.query.filter_by(membership_id=membership.id, status="Pending").first()
    if pending:
        return pending
    payment = SimulatedPayment(organization_id=membership.organization_id, membership_id=membership.id,
                               plan_id=plan.id, amount_minor=amount_minor, kind=kind,
                               created_day=simulated_day())
    db.session.add(payment)
    db.session.flush()
    record_activity("simulated_payment_created", "payment", payment.id,
                    f"{kind}; LKR {amount_minor / 100:.2f}; simulated only")
    db.session.commit()
    return payment


def settle_simulated_payment(payment: SimulatedPayment) -> None:
    """Apply a simulated bank confirmation once; repeated confirmations are harmless."""
    if payment.status != "Pending":
        record_activity("simulated_payment_duplicate_ignored", "payment", payment.id,
                        f"Existing status: {payment.status}")
        db.session.commit()
        return
    membership = db.session.get(Membership, payment.membership_id)
    payment.status = "Paid"
    payment.confirmed_day = simulated_day()
    if payment.kind in ("join", "renewal"):
        plan = db.session.get(Plan, payment.plan_id)
        membership.plan_id = plan.id
        membership.status = "Active"
        if payment.kind == "join" or membership.start_day is None:
            membership.start_day = simulated_day()
        membership.end_day = simulated_day() + period_days(plan)
        membership.cancel_at_period_end = False
        if payment.kind == "renewal":
            grant_loyalty_rewards(membership.organization, simulated_day(), simulated_day(),
                                  include_already_qualified=True)
    elif payment.kind == "upgrade":
        membership.plan_id = payment.plan_id
    action = "offline_payment_recorded" if payment.channel == "offline" else "simulated_payment_confirmed"
    details = f"{payment.kind}; offline payment recorded by organization" if payment.channel == "offline" else f"{payment.kind}; simulated confirmation"
    record_activity(action, "payment", payment.id, details)
    db.session.commit()


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
        def wrapped(*args, **kwargs):
            if not getattr(g, "user", None):
                endpoint = "member_sign_in" if roles == ("member",) else "login"
                return redirect(url_for(endpoint, next=request.path))
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
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", ""),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "1") == "1",
        MAIL_USE_SSL=os.environ.get("MAIL_USE_SSL", "0") == "1",
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5000"),
        DEV_EMAIL_PREVIEW=os.environ.get("DEV_EMAIL_PREVIEW", "0") == "1",
        SHOW_PLATFORM_ADMIN_LINK=os.environ.get("SHOW_PLATFORM_ADMIN_LINK", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("ORGANIZATION_LOGO_FOLDER", os.environ.get(
        "ORGANIZATION_LOGO_FOLDER", os.path.join(app.instance_path, "organization-logos")))
    os.makedirs(app.config["ORGANIZATION_LOGO_FOLDER"], exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Set SECRET_KEY in the environment before starting the app.")
    public_url = urlsplit(app.config.get("PUBLIC_BASE_URL", ""))
    if public_url.scheme not in ("http", "https") or not public_url.hostname or public_url.username or public_url.password:
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTP or HTTPS URL without credentials.")
    if app.config.get("DEV_EMAIL_PREVIEW"):
        if public_url.scheme != "http" or public_url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise RuntimeError("DEV_EMAIL_PREVIEW is only allowed with a localhost HTTP PUBLIC_BASE_URL.")

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    with app.app_context():
        ensure_local_sqlite_schema()

    def issue_email_verification(user: User) -> bool:
        if user.email_verified:
            return True
        smtp_configured = all(app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM"))
        if not smtp_configured and not app.config.get("DEV_EMAIL_PREVIEW"):
            return False
        previous_tokens = EmailVerificationToken.query.filter_by(user_id=user.id).order_by(
            EmailVerificationToken.created_at.desc()).all()
        if previous_tokens:
            last_sent = previous_tokens[0].created_at
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            if utcnow() - last_sent < timedelta(minutes=2):
                return False
        for previous in previous_tokens:
            previous.used_at = utcnow()
        raw_token = secrets.token_urlsafe(32)
        db.session.add(EmailVerificationToken(
            user_id=user.id, token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=utcnow() + timedelta(hours=24)))
        db.session.commit()
        path = url_for("confirm_email", token=raw_token)
        base_url = app.config["PUBLIC_BASE_URL"].rstrip("/")
        confirmation_url = f"{base_url}{path}"
        message = EmailMessage()
        message["Subject"] = "Confirm your Socio email address"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = user.email
        message.set_content(
            f"Hi {user.full_name or 'there'},\n\nConfirm your email address for Socio by opening this link:\n"
            f"{confirmation_url}\n\nThe link expires in 24 hours. If you did not create this account, you can ignore this message."
        )
        if not smtp_configured:
            app.logger.warning("LOCAL EMAIL PREVIEW for %s — copy this link into your browser:\n%s",
                               user.email, confirmation_url)
            return True
        try:
            server = app.config["MAIL_SERVER"]
            port = int(app.config.get("MAIL_PORT", 587))
            if app.config.get("MAIL_USE_SSL"):
                smtp = smtplib.SMTP_SSL(server, port, timeout=15)
            else:
                smtp = smtplib.SMTP(server, port, timeout=15)
            with smtp:
                if app.config.get("MAIL_USE_TLS") and not app.config.get("MAIL_USE_SSL"):
                    smtp.starttls()
                username = app.config.get("MAIL_USERNAME")
                password = app.config.get("MAIL_PASSWORD")
                if username:
                    smtp.login(username, password or "")
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException, ValueError):
            return False

    def verification_expired(verification: EmailVerificationToken) -> bool:
        expires_at = verification.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= utcnow()

    def email_preview_mode() -> bool:
        return bool(app.config.get("DEV_EMAIL_PREVIEW") and not all(
            app.config.get(key) for key in ("MAIL_SERVER", "MAIL_FROM")))

    @app.before_request
    def load_user():
        g.user = db.session.get(User, session.get("user_id")) if session.get("user_id") else None
        if g.user and (not g.user.active or not g.user.session_nonce or not hmac.compare_digest(
                g.user.session_nonce, session.get("session_nonce", ""))):
            session.clear()
            g.user = None
        g.simulated_day = simulated_day()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        if getattr(g, "user", None):
            response.headers.setdefault("Cache-Control", "no-store")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.context_processor
    def template_helpers():
        return {"membership_status": membership_status}

    @app.get("/")
    def index():
        if g.user:
            return redirect(url_for("welcome"))
        organizations = Organization.query.filter_by(status="Approved").order_by(Organization.name).all()
        return render_template("index.html", organizations=organizations)

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.get("/healthz")
    def health_check():
        try:
            db.session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            db.session.rollback()
            return Response("unhealthy\n", status=503, mimetype="text/plain")
        return Response("ok\n", mimetype="text/plain")

    @app.get("/service-worker.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "service-worker.js",
                                       mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/welcome")
    @login_required
    def welcome():
        memberships = Membership.query.filter_by(user_id=g.user.id).order_by(Membership.id.desc()).all() if g.user.role == "member" else []
        return render_template("welcome.html", membership=memberships[0] if memberships else None,
                               memberships=memberships)

    @app.route("/email/confirm/<token>", methods=["GET", "POST"])
    @limiter.limit("20 per hour")
    def confirm_email(token: str):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        verification = EmailVerificationToken.query.filter_by(token_hash=token_hash).first()
        if request.method == "GET":
            valid = bool(verification and not verification.used_at and not verification_expired(verification))
            return render_template("confirm_email.html", valid=valid)
        if not verification or verification.used_at or verification_expired(verification):
            flash("That confirmation link is invalid or expired. Request a new one to continue.", "error")
            return redirect(url_for("resend_email_confirmation"))
        verification.used_at = utcnow()
        verification.user.email_verified = True
        db.session.commit()
        establish_session(verification.user)
        flash("Your email is confirmed. Welcome to Socio.", "success")
        return redirect(url_for("welcome"))

    @app.route("/email/resend", methods=["GET", "POST"])
    @limiter.limit("5 per hour")
    def resend_email_confirmation():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            users = User.query.filter_by(email=email, active=True, email_verified=False).all() if valid_email(email) else []
            for user in users:
                issue_email_verification(user)
            # Keep the resend response identical whether or not an account exists.
            if email_preview_mode():
                flash("If an unconfirmed Socio account uses that email, its local confirmation link is printed in the Flask server console.", "success")
            else:
                flash("If an unconfirmed Socio account uses that email and email delivery is configured, a fresh link will arrive shortly. Otherwise ask the site administrator to check SMTP settings.", "success")
            return redirect(url_for("resend_email_confirmation"))
        return render_template("resend_email.html", preview=email_preview_mode())

    @app.get("/organizations/<int:organization_id>")
    def public_organization(organization_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        plans = Plan.query.filter_by(organization_id=organization.id, archived=False).order_by(Plan.price_minor).all()
        offers = [offer for offer in current_organization_offers(organization.id)
                  if not offer.plan_id or any(plan.id == offer.plan_id for plan in plans)]
        events = OrganizationEvent.query.filter_by(organization_id=organization.id, archived=False).filter(
            OrganizationEvent.event_date >= utcnow().date()).order_by(OrganizationEvent.event_date).all()
        return render_template("public_organization.html", organization=organization, plans=plans,
                               offers=offers, events=events, simulated_day=simulated_day())

    @app.get("/organizations/<int:organization_id>/offers/<int:offer_id>")
    def public_organization_offer(organization_id: int, offer_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        offer = OrganizationOffer.query.filter_by(
            id=offer_id, organization_id=organization.id, archived=False
        ).first_or_404()
        current_day = simulated_day()
        if (offer.start_day > current_day
                or (offer.end_day is not None and offer.end_day < current_day)
                or (offer.plan_id and (not offer.plan or offer.plan.archived))):
            abort(404)
        return render_template("offer_detail.html", organization=organization,
                               offer=offer, simulated_day=current_day)

    @app.get("/organizations/<int:organization_id>/events/<int:event_id>")
    def public_organization_event(organization_id: int, event_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        event = OrganizationEvent.query.filter_by(
            id=event_id, organization_id=organization.id, archived=False
        ).first_or_404()
        if event.event_date < utcnow().date():
            abort(404)
        plans = Plan.query.filter_by(organization_id=organization.id, archived=False).order_by(
            Plan.price_minor).all()
        return render_template("event_detail.html", organization=organization,
                               event=event, plans=plans)

    @app.get("/organizations/<int:organization_id>/logo")
    def organization_logo(organization_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or not organization.logo_filename:
            abort(404)
        if (organization.status != "Approved" and not
                (g.user and g.user.role == "organization_admin"
                 and g.user.organization_id == organization.id)):
            abort(404)
        return send_from_directory(app.config["ORGANIZATION_LOGO_FOLDER"], organization.logo_filename,
                                   mimetype="image/png", max_age=3600, conditional=True)

    @app.get("/events/<int:event_id>/poster")
    def organization_event_poster(event_id: int):
        event = db.session.get(OrganizationEvent, event_id)
        if not event or not event.poster_filename:
            abort(404)
        organization = event.organization
        if (organization.status != "Approved" and not
                (g.user and g.user.role == "organization_admin"
                 and g.user.organization_id == organization.id)):
            abort(404)
        return send_from_directory(app.config["ORGANIZATION_LOGO_FOLDER"], event.poster_filename,
                                   mimetype="image/png", max_age=3600, conditional=True)

    @app.get("/members")
    def member_directory():
        search = request.args.get("q", "").strip()[:100]
        organization_query = Organization.query.filter_by(status="Approved")
        if search:
            needle = f"%{search}%"
            matching = db.session.query(Organization.id).outerjoin(
                Plan, (Plan.organization_id == Organization.id) & (Plan.archived.is_(False))
            ).outerjoin(
                OrganizationOffer,
                (OrganizationOffer.organization_id == Organization.id) & (OrganizationOffer.archived.is_(False))
                & (OrganizationOffer.start_day <= simulated_day())
                & (or_(OrganizationOffer.end_day.is_(None), OrganizationOffer.end_day >= simulated_day()))
            ).outerjoin(
                OrganizationEvent,
                (OrganizationEvent.organization_id == Organization.id)
                & (OrganizationEvent.archived.is_(False))
                & (OrganizationEvent.event_date >= utcnow().date())
            ).filter(or_(Organization.name.ilike(needle), Plan.name.ilike(needle),
                         Plan.benefits.ilike(needle), OrganizationOffer.title.ilike(needle),
                         OrganizationOffer.description.ilike(needle), OrganizationEvent.title.ilike(needle),
                         OrganizationEvent.description.ilike(needle), OrganizationEvent.venue.ilike(needle))).distinct().subquery()
            organization_query = organization_query.filter(Organization.id.in_(matching))
        organizations = organization_query.order_by(Organization.name).all()
        plan_counts = {org.id: Plan.query.filter_by(organization_id=org.id, archived=False).count()
                       for org in organizations}
        active_offers = OrganizationOffer.query.filter(OrganizationOffer.organization_id.in_(
            [org.id for org in organizations])).filter_by(archived=False).filter(
                OrganizationOffer.start_day <= simulated_day(),
                or_(OrganizationOffer.end_day.is_(None), OrganizationOffer.end_day >= simulated_day())
            ).order_by(OrganizationOffer.id.desc()).all() if organizations else []
        offers_by_org: dict[int, list[OrganizationOffer]] = {}
        for offer in active_offers:
            if offer.plan_id and offer.plan.archived:
                continue
            offers_by_org.setdefault(offer.organization_id, []).append(offer)
        lowest_prices = {org.id: db.session.query(func.min(Plan.price_minor)).filter_by(
            organization_id=org.id, archived=False).scalar() for org in organizations}
        events_by_org: dict[int, list[OrganizationEvent]] = {}
        if organizations:
            upcoming_events = OrganizationEvent.query.filter(
                OrganizationEvent.organization_id.in_([org.id for org in organizations]),
                OrganizationEvent.archived.is_(False),
                OrganizationEvent.event_date >= utcnow().date(),
            ).order_by(OrganizationEvent.event_date).all()
            for event in upcoming_events:
                events_by_org.setdefault(event.organization_id, []).append(event)
        return render_template("member_directory.html", organizations=organizations,
                               plan_counts=plan_counts, offers_by_org=offers_by_org,
                               lowest_prices=lowest_prices, events_by_org=events_by_org, search=search)

    @app.route("/organizations/register", methods=["GET", "POST"])
    @limiter.limit("5 per hour")
    def register_organization():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            org_email = request.form.get("organization_email", "").strip().lower()
            admin_email = request.form.get("email", "").strip().lower()
            admin_name = request.form.get("admin_name", "").strip()
            gender = request.form.get("gender", "").strip()
            password = request.form.get("password", "")
            if (not name or len(name) > 120 or not admin_name or len(admin_name) > 120
                    or gender not in ("", "Woman", "Man", "Non-binary", "Prefer not to say", "Other")
                    or not valid_email(org_email) or not valid_email(admin_email)):
                flash("Enter a valid organization name, administrator name, and email addresses.", "error")
            elif len(password) < 12 or len(password) > 128:
                flash("Use a password between 12 and 128 characters.", "error")
            elif Organization.query.filter_by(email=org_email).first():
                flash("That organization contact email is already registered.", "error")
            else:
                organization = Organization(name=name, email=org_email)
                db.session.add(organization)
                db.session.flush()
                user = User(email=admin_email, full_name=admin_name, gender=gender or None,
                            password_hash=password_hasher.hash(password),
                            role="organization_admin", organization_id=organization.id,
                            email_verified=False)
                db.session.add(user)
                db.session.flush()
                record_activity("organization_registered", "organization", organization.id)
                db.session.commit()
                sent = issue_email_verification(user)
                return render_template("email_pending.html", sent=sent, preview=email_preview_mode(), email=user.email)
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def login():
        login_role = request.args.get("role", "")
        if login_role not in ("platform_admin", "organization_admin", "member"):
            login_role = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            organization_email = request.form.get("organization_email", "").strip().lower()
            candidates = User.query.filter_by(email=email).all()
            if login_role:
                role_candidates = [u for u in candidates if u.role == login_role]
                if organization_email:
                    organization = Organization.query.filter_by(email=organization_email).first()
                    user = next((u for u in role_candidates if organization and
                                 (u.organization_id == organization.id or
                                  (u.role == "member" and Membership.query.filter_by(
                                      user_id=u.id, organization_id=organization.id).first()))), None)
                else:
                    user = role_candidates[0] if len(role_candidates) == 1 else None
            elif organization_email:
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
            elif not user.email_verified:
                flash("Confirm your email before signing in. You can request another link below.", "error")
                return redirect(url_for("resend_email_confirmation"))
            elif (user.role == "organization_admin" and user.organization_id is not None
                  and user.organization.status != "Approved"):
                flash("This organization is not currently approved for sign-in.", "error")
            else:
                establish_session(user)
                flash(f"Welcome back, {user.full_name or user.email}.", "success")
                return redirect(url_for("welcome"))
        return render_template("login.html", login_role=login_role)

    @app.route("/member/sign-in", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def member_sign_in():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            organization_email = request.form.get("organization_email", "").strip().lower()
            candidates = User.query.filter_by(email=email, role="member", active=True).all()
            if organization_email:
                organization = Organization.query.filter_by(email=organization_email, status="Approved").first()
                user = next((u for u in candidates if organization and
                             (u.organization_id == organization.id or Membership.query.filter_by(
                                 user_id=u.id, organization_id=organization.id).first())), None)
            else:
                user = candidates[0] if len(candidates) == 1 else None
            password = request.form.get("password", "")
            try:
                valid = bool(user and password_hasher.verify(user.password_hash, password))
            except VerifyMismatchError:
                valid = False
            if not valid:
                flash("Email or password is incorrect. If this email is linked to older separate organization accounts, include an organization contact email.", "error")
            elif not user.email_verified:
                flash("Confirm your email before signing in. You can request another link below.", "error")
                return redirect(url_for("resend_email_confirmation"))
            else:
                establish_session(user)
                flash(f"Welcome back, {user.full_name or user.email}.", "success")
                return redirect(url_for("welcome"))
        organizations = Organization.query.filter_by(status="Approved").order_by(Organization.name).all()
        return render_template("member_sign_in.html", organizations=organizations)

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
        member_counts = {org.id: Membership.query.filter_by(organization_id=org.id).count() for org in organizations}
        pending_payments = SimulatedPayment.query.filter_by(status="Pending").order_by(SimulatedPayment.id.desc()).all()
        payments = SimulatedPayment.query.order_by(SimulatedPayment.id.desc()).limit(50).all()
        pending_refunds = SimulatedRefund.query.filter_by(status="Requested").order_by(SimulatedRefund.id.desc()).all()
        activity = ActivityLog.query.order_by(ActivityLog.id.desc()).limit(20).all()
        paid_total_minor = db.session.query(func.coalesce(func.sum(SimulatedPayment.amount_minor), 0)).filter_by(status="Paid").scalar()
        refunded_total_minor = db.session.query(func.coalesce(func.sum(SimulatedRefund.amount_minor), 0)).filter_by(status="Approved").scalar()
        return render_template("admin_organizations.html", organizations=organizations,
                               member_counts=member_counts,
                               pending_payments=pending_payments, payments=payments,
                               pending_refunds=pending_refunds, activity=activity,
                               member_total=Membership.query.count(), payment_total=SimulatedPayment.query.count(),
                               paid_total_minor=paid_total_minor, refunded_total_minor=refunded_total_minor,
                               net_total_minor=paid_total_minor - refunded_total_minor,
                               simulated_day=simulated_day())

    @app.get("/admin/reconciliation.csv")
    @role_required("platform_admin")
    def admin_reconciliation_export():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["payment_id", "organization", "member", "email", "plan", "type", "channel",
                         "payment_status", "gross_minor", "refund_status", "refund_minor", "net_minor",
                         "created_simulated_day", "confirmed_simulated_day"])
        payments = SimulatedPayment.query.order_by(SimulatedPayment.id.asc()).all()
        for payment in payments:
            refund = payment.refund_request
            paid = payment.status == "Paid"
            refunded = bool(refund and refund.status == "Approved")
            writer.writerow([payment.id, spreadsheet_safe(payment.organization.name),
                             spreadsheet_safe(payment.membership.full_name), spreadsheet_safe(payment.membership.user.email),
                             spreadsheet_safe(payment.plan.name), payment.kind, payment.channel, payment.status,
                             payment.amount_minor if paid else 0,
                             refund.status if refund else "", refund.amount_minor if refunded else 0,
                             (payment.amount_minor if paid else 0) - (refund.amount_minor if refunded else 0),
                             payment.created_day, payment.confirmed_day if payment.confirmed_day is not None else ""])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="socio-simulated-reconciliation.csv"'
        return response

    @app.get("/admin")
    @app.get("/platform-admin")
    def admin_entry():
        """Private entry point for the platform-admin workspace."""
        if g.user and g.user.role == "platform_admin":
            return redirect(url_for("admin_organizations"))
        return redirect(url_for("login", role="platform_admin"))

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

    @app.post("/admin/payments/<int:payment_id>/confirm")
    @role_required("platform_admin")
    def admin_confirm_payment(payment_id: int):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment:
            abort(404)
        settle_simulated_payment(payment)
        flash(f"Simulated payment #{payment.id} confirmation delivered.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/refunds/<int:refund_id>/<action>")
    @role_required("platform_admin")
    def admin_process_refund(refund_id: int, action: str):
        refund = db.session.get(SimulatedRefund, refund_id)
        if not refund:
            abort(404)
        if action not in ("approve", "decline"):
            abort(404)
        if refund.status != "Requested":
            abort(409)
        refund.status = "Approved" if action == "approve" else "Declined"
        refund.processed_by_user_id = g.user.id
        refund.processed_day = simulated_day()
        record_activity(f"simulated_refund_{action}d" if action == "decline" else "simulated_refund_approved",
                        "refund", refund.id,
                        f"LKR {refund.amount_minor / 100:.2f}; simulated only; payment #{refund.payment_id}")
        db.session.commit()
        flash(f"Simulated refund #{refund.id} {refund.status.lower()}. No money was moved.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/simulation/advance")
    @role_required("platform_admin")
    def admin_advance_simulation():
        try:
            days = int(request.form.get("days", "15"))
        except ValueError:
            abort(400)
        if days not in (7, 15):
            abort(400)
        state = db.session.get(SimulationState, 1)
        if not state:
            state = SimulationState(id=1, current_day=0)
            db.session.add(state)
        previous_day = state.current_day
        state.current_day += days
        renewal_count = 0
        downgrade_count = 0
        loyalty_reward_count = 0
        for organization in Organization.query.filter_by(loyalty_reward_enabled=True).all():
            loyalty_reward_count += grant_loyalty_rewards(organization, previous_day, state.current_day)
        for membership in Membership.query.filter_by(status="Active").all():
            if membership.end_day is None or membership.end_day > state.current_day:
                continue
            if membership.next_plan_id and not membership.cancel_at_period_end:
                membership.plan_id = membership.next_plan_id
                membership.next_plan_id = None
                downgrade_count += 1
                record_activity("membership_downgrade_applied", "membership", membership.id,
                                f"Plan changed at period end on simulated day {state.current_day}")
            if membership.cancel_at_period_end:
                continue
            if membership.last_renewal_due_day == membership.end_day:
                continue
            payment = create_simulated_payment(membership, membership.plan, "renewal", membership.plan.price_minor)
            if payment.kind == "renewal":
                membership.last_renewal_due_day = membership.end_day
                renewal_count += 1
        record_activity("simulation_advanced", "simulation", state.id,
                        f"Advanced {days} days to day {state.current_day}; {renewal_count} renewal(s) due; {downgrade_count} downgrade(s) applied; {loyalty_reward_count} loyalty gift(s) awarded")
        db.session.commit()
        flash(f"Simulation advanced {days} days to day {state.current_day}. {renewal_count} renewal payment(s) are due; {downgrade_count} scheduled downgrade(s) took effect; {loyalty_reward_count} loyalty gift(s) were awarded.", "success")
        return redirect(url_for("admin_organizations"))

    @app.post("/admin/simulation/reset")
    @role_required("platform_admin")
    def admin_reset_simulation():
        # Preserve platform administrator accounts while clearing demo-only data.
        if request.form.get("confirm_reset") != "yes":
            flash("Select the confirmation box before resetting demo data.", "error")
            return redirect(url_for("admin_organizations"))
        ActivityLog.query.delete(synchronize_session=False)
        MemberInvitation.query.delete(synchronize_session=False)
        MembershipApplication.query.delete(synchronize_session=False)
        OrganizationEvent.query.delete(synchronize_session=False)
        EmailVerificationToken.query.delete(synchronize_session=False)
        OrganizationOffer.query.delete(synchronize_session=False)
        SimulatedRefund.query.delete(synchronize_session=False)
        SimulatedPayment.query.delete(synchronize_session=False)
        Membership.query.delete(synchronize_session=False)
        Plan.query.delete(synchronize_session=False)
        User.query.filter(User.role != "platform_admin").delete(synchronize_session=False)
        Organization.query.delete(synchronize_session=False)
        state = db.session.get(SimulationState, 1)
        if state:
            state.current_day = 0
        else:
            db.session.add(SimulationState(id=1, current_day=0))
        record_activity("simulation_reset", "simulation", 1,
                        "Demo data removed; platform administrator accounts preserved")
        db.session.commit()
        for filename in os.listdir(app.config["ORGANIZATION_LOGO_FOLDER"]):
            file_path = os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        flash("Demo data and simulated time were reset. Platform admin accounts were kept.", "success")
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
        search = request.args.get("q", "").strip()[:100]
        member_query = Membership.query.filter_by(organization_id=organization.id)
        if search:
            needle = f"%{search}%"
            member_query = member_query.join(User, Membership.user_id == User.id).filter(
                or_(Membership.full_name.ilike(needle), User.email.ilike(needle)))
        memberships = member_query.order_by(Membership.id.desc()).all()
        offline_eligible = set()
        for membership in memberships:
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if (organization.status == "Approved" and not membership.cancel_at_period_end
                    and membership_status(membership) in ("Pending", "Expired")
                    and not (application and application.status == "Pending")
                    and not SimulatedPayment.query.filter_by(membership_id=membership.id, status="Pending").first()):
                offline_eligible.add(membership.id)
        payments = SimulatedPayment.query.filter_by(organization_id=organization.id).order_by(SimulatedPayment.id.desc()).limit(50).all()
        invitations = MemberInvitation.query.filter_by(organization_id=organization.id).order_by(MemberInvitation.id.desc()).limit(100).all()
        refunds = SimulatedRefund.query.filter_by(organization_id=organization.id).order_by(SimulatedRefund.id.desc()).limit(50).all()
        gross_paid_minor = db.session.query(func.coalesce(func.sum(SimulatedPayment.amount_minor), 0)).filter_by(
            organization_id=organization.id, status="Paid").scalar()
        refund_total_minor = db.session.query(func.coalesce(func.sum(SimulatedRefund.amount_minor), 0)).filter_by(
            organization_id=organization.id, status="Approved").scalar()
        offers = OrganizationOffer.query.filter_by(organization_id=organization.id).order_by(
            OrganizationOffer.id.desc()).all()
        applications = (MembershipApplication.query.join(Membership).filter(
            Membership.organization_id == organization.id).order_by(
                MembershipApplication.status.asc(), MembershipApplication.submitted_at.desc()).limit(100).all())
        events = OrganizationEvent.query.filter_by(organization_id=organization.id).order_by(
            OrganizationEvent.event_date.desc()).limit(100).all()
        return render_template("plans.html", organization=organization, plans=plans,
                               memberships=memberships, payments=payments, simulated_day=simulated_day(),
                               invitations=invitations, offers=offers,
                               offer_types=("Welcome bonus", "Extra benefit", "Limited time", "Other"),
                               search=search, offline_eligible=offline_eligible, refunds=refunds,
                               applications=applications,
                               events=events,
                               today_date=utcnow().date().isoformat(),
                               gross_paid_minor=gross_paid_minor, refund_total_minor=refund_total_minor,
                               net_paid_minor=gross_paid_minor - refund_total_minor)

    @app.post("/organization/loyalty-settings")
    @role_required("organization_admin")
    def organization_loyalty_settings():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        enabled = request.form.get("loyalty_reward_enabled") == "yes"
        try:
            after_days = int(request.form.get("loyalty_reward_after_days", "30"))
            reward_days = int(request.form.get("loyalty_reward_days", "30"))
        except (TypeError, ValueError):
            flash("Enter valid loyalty milestone and gift days.", "error")
            return redirect(url_for("organization_plans"))
        if after_days not in (30, 60, 90, 180, 365) or reward_days not in (7, 14, 30, 60):
            flash("Choose a listed active-support milestone and loyalty gift length.", "error")
            return redirect(url_for("organization_plans"))
        organization.loyalty_reward_enabled = enabled
        organization.loyalty_reward_after_days = after_days
        organization.loyalty_reward_days = reward_days
        rewarded = grant_loyalty_rewards(organization, simulated_day(), simulated_day(),
                                         include_already_qualified=True)
        record_activity("loyalty_reward_settings_changed", "organization", organization.id,
                        f"enabled={enabled}; after={after_days}; gift={reward_days} days; awarded={rewarded}")
        db.session.commit()
        message = "Loyalty gift settings saved."
        if rewarded:
            message += f" {rewarded} currently active member(s) received the configured gift."
        flash(message, "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/events")
    @role_required("organization_admin")
    @limiter.limit("20 per hour")
    def organization_create_event():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        venue = request.form.get("venue", "").strip()
        try:
            event_date = date.fromisoformat(request.form.get("event_date", ""))
        except ValueError:
            event_date = None
        if (not title or len(title) > 120 or not description or len(description) > 1000
                or len(venue) > 200 or event_date is None or event_date < utcnow().date()):
            flash("Enter an event name, future date, and valid event details.", "error")
            return redirect(url_for("organization_plans"))
        poster = request.files.get("poster_file")
        poster_filename = None
        if poster and poster.filename:
            raw = poster.stream.read(5_000_001)
            if len(raw) > 5_000_000:
                flash("Event posters must be no larger than 5 MB.", "error")
                return redirect(url_for("organization_plans"))
            try:
                normalized = normalize_uploaded_image(raw, max_side=1800, max_output_bytes=4_000_000)
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
                flash(str(exc) or "Choose a valid PNG, JPEG, or WebP event poster.", "error")
                return redirect(url_for("organization_plans"))
            poster_filename = f"{secrets.token_hex(20)}.png"
            with open(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], poster_filename), "wb") as poster_file:
                poster_file.write(normalized)
        event = OrganizationEvent(organization_id=organization.id, title=title,
                                  description=description, venue=venue,
                                  event_date=event_date, poster_filename=poster_filename)
        db.session.add(event)
        db.session.flush()
        record_activity("organization_event_published", "organization_event", event.id,
                        f"{title}; {event_date.isoformat()}")
        db.session.commit()
        flash("Event published to your public organization page and member gallery.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/events/<int:event_id>/archive")
    @role_required("organization_admin")
    def organization_archive_event(event_id: int):
        event = OrganizationEvent.query.filter_by(id=event_id,
                                                   organization_id=g.user.organization_id).first_or_404()
        event.archived = True
        record_activity("organization_event_archived", "organization_event", event.id, event.title)
        db.session.commit()
        flash("Event removed from member pages.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/logo")
    @role_required("organization_admin")
    def organization_upload_logo():
        organization = db.session.get(Organization, g.user.organization_id)
        upload = request.files.get("logo_file")
        if not upload or not upload.filename:
            flash("Choose a PNG, JPEG, or WebP image to upload.", "error")
            return redirect(url_for("organization_plans"))
        raw = upload.stream.read(1_500_001)
        if len(raw) > 1_500_000:
            flash("The logo image must be no larger than 1.5 MB.", "error")
            return redirect(url_for("organization_plans"))
        try:
            normalized_bytes = normalize_uploaded_image(raw, max_side=1200, max_output_bytes=2_000_000)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            flash(str(exc) or "That image could not be opened. Choose a valid PNG, JPEG, or WebP logo.", "error")
            return redirect(url_for("organization_plans"))
        filename = f"{secrets.token_hex(20)}.png"
        with open(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], filename), "wb") as image_file:
            image_file.write(normalized_bytes)
        previous_filename = organization.logo_filename
        organization.logo_filename = filename
        record_activity("organization_logo_updated", "organization", organization.id)
        db.session.commit()
        if previous_filename:
            try:
                os.remove(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], previous_filename))
            except FileNotFoundError:
                pass
        flash("Organization logo updated. It now appears in the member gallery and public page.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/logo/remove")
    @role_required("organization_admin")
    def organization_remove_logo():
        organization = db.session.get(Organization, g.user.organization_id)
        previous_filename = organization.logo_filename
        organization.logo_filename = None
        record_activity("organization_logo_removed", "organization", organization.id)
        db.session.commit()
        if previous_filename:
            try:
                os.remove(os.path.join(app.config["ORGANIZATION_LOGO_FOLDER"], previous_filename))
            except FileNotFoundError:
                pass
        flash("Organization logo removed.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/application-settings")
    @role_required("organization_admin")
    def organization_application_settings():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        mode = request.form.get("membership_mode", "")
        if mode not in ("approval", "open"):
            abort(400)
        prompt = request.form.get("application_prompt", "").strip()
        if len(prompt) > 500:
            flash("Application instructions can be up to 500 characters.", "error")
            return redirect(url_for("organization_plans"))
        recommendation_required = request.form.get("recommendation_required") == "yes"
        organization.membership_approval_required = mode == "approval" or recommendation_required
        organization.recommendation_required = recommendation_required
        organization.application_prompt = prompt
        record_activity("membership_application_settings_changed", "organization", organization.id,
                        f"approval={organization.membership_approval_required}; recommendation={recommendation_required}")
        db.session.commit()
        flash("Membership application settings saved.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/applications/<int:application_id>/<action>")
    @role_required("organization_admin")
    def organization_review_application(application_id: int, action: str):
        if action not in ("approve", "decline"):
            abort(404)
        application = (MembershipApplication.query.join(Membership).filter(
            MembershipApplication.id == application_id,
            Membership.organization_id == g.user.organization_id).first_or_404())
        if application.status != "Pending":
            abort(409)
        note = request.form.get("review_note", "").strip()
        if len(note) > 500:
            flash("The review note can be up to 500 characters.", "error")
            return redirect(url_for("organization_plans"))
        application.status = "Approved" if action == "approve" else "Declined"
        application.review_note = note
        application.reviewed_at = utcnow()
        application.reviewed_by_user_id = g.user.id
        if action == "approve":
            payment = create_simulated_payment(application.membership,
                                               application.membership.plan,
                                               "join", application.membership.plan.price_minor)
            record_activity("membership_application_approved", "membership_application", application.id,
                            f"Payment #{payment.id} is ready for member confirmation")
            db.session.commit()
            flash("Application approved. The member can now review and complete the simulated payment.", "success")
        else:
            record_activity("membership_application_declined", "membership_application", application.id, note)
            db.session.commit()
            flash("Application declined. The member can revise and resubmit it.", "info")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/payments/<int:payment_id>/refund")
    @role_required("organization_admin")
    def organization_request_refund(payment_id: int):
        organization = db.session.get(Organization, g.user.organization_id)
        payment = SimulatedPayment.query.filter_by(id=payment_id, organization_id=organization.id).first_or_404()
        reason = request.form.get("reason", "").strip()
        if payment.status != "Paid" or payment.channel != "simulated" or payment.amount_minor <= 0:
            abort(400)
        if SimulatedRefund.query.filter_by(payment_id=payment.id).first():
            flash("A refund request already exists for this payment.", "error")
            return redirect(url_for("organization_plans"))
        if len(reason) < 8 or len(reason) > 500:
            flash("Add a short reason between 8 and 500 characters.", "error")
            return redirect(url_for("organization_plans"))
        refund = SimulatedRefund(payment_id=payment.id, organization_id=organization.id,
                                 amount_minor=payment.amount_minor, reason=reason,
                                 requested_by_user_id=g.user.id, created_day=simulated_day())
        db.session.add(refund)
        db.session.flush()
        record_activity("simulated_refund_requested", "refund", refund.id,
                        f"LKR {refund.amount_minor / 100:.2f}; payment #{payment.id}; simulated only")
        db.session.commit()
        flash("Full simulated refund requested for platform review. No money was moved.", "success")
        return redirect(url_for("organization_plans"))

    @app.get("/organization/members/export.csv")
    @role_required("organization_admin")
    def organization_export_members():
        organization = db.session.get(Organization, g.user.organization_id)
        search = request.args.get("q", "").strip()[:100]
        query = Membership.query.filter_by(organization_id=organization.id)
        if search:
            needle = f"%{search}%"
            query = query.join(User, Membership.user_id == User.id).filter(
                or_(Membership.full_name.ilike(needle), User.email.ilike(needle)))
        rows = query.order_by(Membership.id.desc()).all()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["member_name", "email", "plan", "membership_status", "billing_period",
                         "start_day", "end_day", "scheduled_plan", "cancel_at_period_end"])
        for membership in rows:
            writer.writerow([spreadsheet_safe(membership.full_name), spreadsheet_safe(membership.user.email),
                             spreadsheet_safe(membership.plan.name), spreadsheet_safe(membership_status(membership)),
                             membership.plan.billing_period,
                             membership.start_day if membership.start_day is not None else "",
                             membership.end_day if membership.end_day is not None else "",
                             spreadsheet_safe(membership.next_plan.name if membership.next_plan else ""),
                             "yes" if membership.cancel_at_period_end else "no"])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="socio-members-{organization.id}.csv"'
        return response

    @app.get("/organization/members/import-template.csv")
    @role_required("organization_admin")
    def organization_member_import_template():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["full_name", "email", "plan_name"])
        writer.writerow(["A member name", "member@example.com", "Community"])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="socio-member-import-template.csv"'
        return response

    @app.post("/organization/members/import")
    @role_required("organization_admin")
    @limiter.limit("10 per hour")
    def organization_import_members():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("organization_plans"))
        try:
            raw_data = upload.stream.read(2 * 1024 * 1024 + 1)
            text_data = raw_data.decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("The CSV must use UTF-8 encoding.", "error")
            return redirect(url_for("organization_plans"))
        if len(raw_data) > 2 * 1024 * 1024:
            flash("The CSV is larger than the 2 MB limit.", "error")
            return redirect(url_for("organization_plans"))
        reader = csv.DictReader(io.StringIO(text_data))
        headers = {str(header or "").strip().lower() for header in (reader.fieldnames or [])}
        if not {"full_name", "email", "plan_name"}.issubset(headers):
            flash("Include these CSV columns: full_name, email, plan_name.", "error")
            return redirect(url_for("organization_plans"))
        rows = list(reader)
        if not rows or len(rows) > 500:
            flash("Import between 1 and 500 member rows at a time.", "error")
            return redirect(url_for("organization_plans"))
        plans_by_name = {}
        for plan in Plan.query.filter_by(organization_id=organization.id, archived=False).all():
            plans_by_name.setdefault(plan.name.strip().casefold(), []).append(plan)
        normalized, errors, seen = [], [], set()
        today = simulated_day()
        for line_number, raw in enumerate(rows, start=2):
            row = {str(key or "").strip().lower(): (value or "").strip()
                   for key, value in raw.items() if key is not None}
            name = row.get("full_name", "")
            email = row.get("email", "").lower()
            matches = plans_by_name.get(row.get("plan_name", "").casefold(), [])
            issue = None
            if not name or len(name) > 120:
                issue = "name is required (up to 120 characters)"
            elif not valid_email(email):
                issue = "email is invalid"
            elif email in seen:
                issue = "email appears more than once in this CSV"
            elif User.query.filter_by(email=email, organization_id=organization.id).first():
                issue = "an account already exists for this email"
            elif MemberInvitation.query.filter_by(organization_id=organization.id, email=email,
                                                   status="Pending").filter(
                    MemberInvitation.expires_day > today).first():
                issue = "an active invitation already exists for this email"
            elif len(matches) != 1:
                issue = "plan name must match one available plan exactly"
            if issue:
                errors.append(f"Row {line_number}: {issue}.")
            else:
                seen.add(email)
                normalized.append((name, email, matches[0]))
        if errors:
            flash("No invitations were created. " + " ".join(errors[:10]), "error")
            return redirect(url_for("organization_plans"))
        invite_links = []
        for name, email, plan in normalized:
            raw_token = secrets.token_urlsafe(32)
            invite = MemberInvitation(
                organization_id=organization.id, plan_id=plan.id, full_name=name, email=email,
                token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), status="Pending",
                created_day=today, expires_day=today + 7, created_by_user_id=g.user.id)
            db.session.add(invite)
            db.session.flush()
            invite_links.append({"name": name, "email": email, "plan": plan.name,
                                 "url": f"{app.config['PUBLIC_BASE_URL'].rstrip('/')}{url_for('claim_member_invitation', token=raw_token)}"})
        record_activity("member_invitations_imported", "organization", organization.id,
                        f"{len(invite_links)} invitation(s); raw invitation tokens are not stored")
        db.session.commit()
        return render_template("invite_results.html", organization=organization, invite_links=invite_links)

    @app.route("/invite/<token>", methods=["GET", "POST"])
    @limiter.limit("20 per hour")
    def claim_member_invitation(token: str):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        invite = MemberInvitation.query.filter_by(token_hash=token_hash).first()
        if (not invite or invite.status != "Pending" or simulated_day() >= invite.expires_day
                or invite.organization.status != "Approved" or invite.plan.archived):
            abort(404)
        if request.method == "POST":
            name = request.form.get("full_name", "").strip()
            gender = request.form.get("gender", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            allowed_genders = ("", "Woman", "Man", "Non-binary", "Prefer not to say", "Other")
            if not name or len(name) > 120 or gender not in allowed_genders:
                flash("Enter your name and choose a valid gender option.", "error")
            elif len(password) < 12 or len(password) > 128 or password != confirm:
                flash("Choose a password from 12 to 128 characters and enter it the same way twice.", "error")
            else:
                existing_org_user = User.query.filter_by(email=invite.email,
                                                         organization_id=invite.organization_id).first()
                if existing_org_user and existing_org_user.role != "member":
                    abort(409)
                existing_members = User.query.filter_by(email=invite.email, role="member").order_by(User.id).all()
                if g.user and g.user.role == "member" and g.user.email == invite.email:
                    member = g.user
                elif existing_members:
                    member = next((candidate for candidate in existing_members
                                   if Membership.query.filter_by(user_id=candidate.id,
                                                                 organization_id=invite.organization_id).first()), None)
                    member = member or (existing_members[0] if len(existing_members) == 1 else None)
                    if member is None:
                        flash("This email has older separate member accounts. Sign in to the intended account, then open the invitation again.", "error")
                        return render_template("member_invite.html", invite=invite)
                    if not (g.user and g.user.id == member.id):
                        try:
                            password_ok = password_hasher.verify(member.password_hash, password)
                        except VerifyMismatchError:
                            password_ok = False
                        if not password_ok:
                            flash("Enter the password for your existing member account to accept this invitation.", "error")
                            return render_template("member_invite.html", invite=invite)
                else:
                    member = User(email=invite.email, full_name=name, gender=gender or None,
                                  password_hash=password_hasher.hash(password), role="member",
                                  organization_id=invite.organization_id, email_verified=False)
                    db.session.add(member)
                    db.session.flush()
                existing_membership = Membership.query.filter_by(user_id=member.id,
                                                                   organization_id=invite.organization_id).first()
                if existing_membership:
                    abort(409)
                member.full_name = name or member.full_name
                if gender:
                    member.gender = gender
                membership = Membership(organization_id=invite.organization_id, user_id=member.id,
                                        plan_id=invite.plan_id, full_name=name)
                db.session.add(membership)
                invite.status = "Claimed"
                invite.claimed_day = simulated_day()
                invite.claimed_user = member
                record_activity("member_invitation_claimed", "invitation", invite.id,
                                "Invitation claimed; member account created")
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    abort(409)
                if invite.organization.membership_approval_required:
                    application = MembershipApplication(
                        membership_id=membership.id,
                        status="Approved",
                        submitted_at=utcnow(),
                        reviewed_at=utcnow(),
                        reviewed_by_user_id=invite.created_by_user_id,
                        review_note="Approved through an organization-issued invitation.",
                    )
                    db.session.add(application)
                    db.session.commit()
                if not member.email_verified:
                    sent = issue_email_verification(member)
                    return render_template("email_pending.html", sent=sent, preview=email_preview_mode(), email=member.email)
                payment = create_simulated_payment(membership, invite.plan, "join", invite.plan.price_minor)
                establish_session(member)
                flash(f"Welcome to {invite.organization.name}, {name}.", "success")
                return redirect(url_for("payment_review", payment_id=payment.id))
        return render_template("member_invite.html", invite=invite)

    @app.post("/organization/invitations/<int:invitation_id>/revoke")
    @role_required("organization_admin")
    def organization_revoke_invitation(invitation_id: int):
        invite = MemberInvitation.query.filter_by(id=invitation_id,
                                                  organization_id=g.user.organization_id).first_or_404()
        if invite.status != "Pending":
            abort(409)
        invite.status = "Revoked"
        record_activity("member_invitation_revoked", "invitation", invite.id)
        db.session.commit()
        flash("Invitation revoked.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/members/<int:membership_id>/record-offline-payment")
    @role_required("organization_admin")
    def organization_record_offline_payment(membership_id: int):
        organization = db.session.get(Organization, g.user.organization_id)
        membership = Membership.query.filter_by(id=membership_id, organization_id=organization.id).first_or_404()
        status = membership_status(membership)
        if organization.status != "Approved" or status not in ("Pending", "Expired"):
            abort(400)
        if status == "Pending" and organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if not application or application.status != "Approved":
                flash("Approve this membership application before recording a join payment.", "error")
                return redirect(url_for("organization_plans"))
        if membership.cancel_at_period_end:
            flash("This member has scheduled cancellation. Ask them to request renewal first.", "error")
            return redirect(url_for("organization_plans"))
        if SimulatedPayment.query.filter_by(membership_id=membership.id, status="Pending").first():
            flash("A payment is already awaiting confirmation. Resolve it before recording an offline payment.", "error")
            return redirect(url_for("organization_plans"))
        plan = membership.plan
        if plan.archived:
            flash("This membership plan is archived. Restore or choose an available plan before recording payment.", "error")
            return redirect(url_for("organization_plans"))
        kind = "join" if status == "Pending" else "renewal"
        payment = SimulatedPayment(organization_id=organization.id, membership_id=membership.id,
                                   plan_id=plan.id, amount_minor=plan.price_minor, kind=kind,
                                   channel="offline", status="Pending", created_day=simulated_day())
        db.session.add(payment)
        db.session.flush()
        settle_simulated_payment(payment)
        flash(f"Offline payment recorded in the demo for {membership.full_name}. No funds were verified or moved.", "success")
        return redirect(url_for("organization_plans"))

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

    @app.post("/organization/offers")
    @role_required("organization_admin")
    def organization_create_offer():
        organization = db.session.get(Organization, g.user.organization_id)
        if organization.status != "Approved":
            abort(403)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        offer_type = request.form.get("offer_type", "")
        allowed_types = ("Welcome bonus", "Extra benefit", "Limited time", "Other")
        try:
            start_day = int(request.form.get("start_day", simulated_day()))
            end_raw = request.form.get("end_day", "").strip()
            end_day = int(end_raw) if end_raw else None
            selected_plan_id = int(request.form.get("plan_id", "")) if request.form.get("plan_id") else None
        except (TypeError, ValueError):
            flash("Choose valid offer dates and plan.", "error")
            return redirect(url_for("organization_plans"))
        plan = (Plan.query.filter_by(id=selected_plan_id, organization_id=organization.id,
                                     archived=False).first() if selected_plan_id else None)
        if (not title or len(title) > 100 or not description or len(description) > 500
                or offer_type not in allowed_types or start_day < simulated_day()
                or (end_day is not None and end_day < start_day)
                or (selected_plan_id is not None and plan is None)):
            flash("Enter a title and description, choose an offer type and available plan, and use valid simulated dates.", "error")
            return redirect(url_for("organization_plans"))
        offer = OrganizationOffer(organization_id=organization.id, plan_id=plan.id if plan else None,
                                  title=title, description=description, offer_type=offer_type,
                                  start_day=start_day, end_day=end_day)
        db.session.add(offer)
        db.session.flush()
        record_activity("organization_offer_created", "offer", offer.id,
                        f"{offer_type}; no checkout discount applied")
        db.session.commit()
        flash("Offer published. It appears in the member gallery during its scheduled days; plan prices remain unchanged.", "success")
        return redirect(url_for("organization_plans"))

    @app.post("/organization/offers/<int:offer_id>/archive")
    @role_required("organization_admin")
    def organization_archive_offer(offer_id: int):
        offer = OrganizationOffer.query.filter_by(id=offer_id,
                                                   organization_id=g.user.organization_id).first_or_404()
        offer.archived = True
        record_activity("organization_offer_archived", "offer", offer.id)
        db.session.commit()
        flash("Offer removed from member pages.", "success")
        return redirect(url_for("organization_plans"))

    @app.route("/join/<int:organization_id>", methods=["GET", "POST"])
    @limiter.limit("10 per minute")
    def join_organization(organization_id: int):
        organization = db.session.get(Organization, organization_id)
        if not organization or organization.status != "Approved":
            abort(404)
        plans = Plan.query.filter_by(organization_id=organization.id, archived=False).order_by(Plan.price_minor).all()
        if not plans:
            flash("This organization has no available plans yet.", "error")
            return redirect(url_for("public_organization", organization_id=organization.id))
        if request.method == "POST":
            name = request.form.get("full_name", "").strip()
            gender = request.form.get("gender", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            recommendation_text = request.form.get("recommendation_text", "").strip()
            try:
                plan_id = int(request.form.get("plan_id", ""))
            except ValueError:
                plan_id = 0
            plan = Plan.query.filter_by(id=plan_id, organization_id=organization.id, archived=False).first()
            current_member = bool(g.user and g.user.role == "member")
            if not plan:
                flash("Enter your name, a valid email, and choose an available plan.", "error")
                return render_template("join.html", organization=organization, plans=plans,
                                       selected_plan_id=plan_id)
            if len(recommendation_text) > 5000 or (organization.recommendation_required and not recommendation_text):
                flash("This organization requires recommendation details of up to 5,000 characters.", "error")
                return render_template("join.html", organization=organization, plans=plans,
                                       selected_plan_id=plan_id)
            if g.user and not current_member:
                flash("Sign out before applying with a member account.", "error")
                return render_template("join.html", organization=organization, plans=plans,
                                       selected_plan_id=plan_id)
            if current_member:
                member = g.user
                previous = Membership.query.filter_by(user_id=member.id, organization_id=organization.id).first()
                name = (previous.full_name if previous else "") or member.full_name or name or member.email.split("@", 1)[0]
                email = member.email
            else:
                if (not name or len(name) > 120
                        or gender not in ("", "Woman", "Man", "Non-binary", "Prefer not to say", "Other")
                        or not valid_email(email)):
                    flash("Enter your name, a valid email address, and a valid gender option.", "error")
                    return render_template("join.html", organization=organization, plans=plans,
                                           selected_plan_id=plan_id)
                organization_account = User.query.filter_by(email=email, organization_id=organization.id).first()
                if organization_account and organization_account.role != "member":
                    flash("That email is already used for this organization account. Sign in or use a different member email.", "error")
                    return render_template("join.html", organization=organization, plans=plans,
                                           selected_plan_id=plan_id)
                member_candidates = User.query.filter_by(email=email, role="member").order_by(User.id).all()
                matching_org_member = next((candidate for candidate in member_candidates
                                            if Membership.query.filter_by(user_id=candidate.id,
                                                                          organization_id=organization.id).first()), None)
                member = matching_org_member or (member_candidates[0] if len(member_candidates) == 1 else None)
                if len(member_candidates) > 1 and member is None:
                    flash("This email has older separate member accounts. Sign in to the intended account before applying.", "error")
                    return render_template("join.html", organization=organization, plans=plans,
                                           selected_plan_id=plan_id)
                if member:
                    try:
                        password_ok = password_hasher.verify(member.password_hash, password)
                    except VerifyMismatchError:
                        password_ok = False
                    if not password_ok:
                        flash("Email or password is incorrect.", "error")
                        return render_template("join.html", organization=organization, plans=plans,
                                               selected_plan_id=plan_id)
                else:
                    if len(password) < 12 or len(password) > 128:
                        flash("Use a password between 12 and 128 characters.", "error")
                        return render_template("join.html", organization=organization, plans=plans,
                                               selected_plan_id=plan_id)
                    member = User(email=email, full_name=name, gender=gender or None,
                                  password_hash=password_hasher.hash(password),
                                  role="member", organization_id=organization.id,
                                  email_verified=False)
                    db.session.add(member)
                    db.session.flush()
            requires_confirmation = not member.email_verified
            membership = Membership.query.filter_by(user_id=member.id, organization_id=organization.id).first()
            application = (MembershipApplication.query.filter_by(membership_id=membership.id).first()
                           if membership else None)
            if membership and not member.email_verified:
                sent = issue_email_verification(member)
                return render_template("email_pending.html", sent=sent, preview=email_preview_mode(), email=member.email)
            if membership and membership_status(membership) in ("Active", "Cancelled", "Pending"):
                if not (application and application.status == "Declined"):
                    if g.user is None:
                        establish_session(member)
                    flash("You already have a membership here. Your membership page has the next steps.", "success")
                    return redirect(url_for("welcome"))
            if membership:
                membership.plan_id = plan.id
                membership.full_name = name
                membership.status = "Pending"
                membership.start_day = None
                membership.end_day = None
                membership.cancel_at_period_end = False
            else:
                membership = Membership(organization_id=organization.id, user_id=member.id,
                                        plan_id=plan.id, full_name=name)
                db.session.add(membership)
                db.session.flush()
            member.full_name = name
            if gender:
                member.gender = gender
            if organization.membership_approval_required:
                if application is None:
                    application = MembershipApplication(membership_id=membership.id)
                    db.session.add(application)
                application.status = "Pending"
                application.recommendation_text = recommendation_text
                application.review_note = ""
                application.submitted_at = utcnow()
                application.reviewed_at = None
                application.reviewed_by_user_id = None
                db.session.flush()
                record_activity("membership_application_submitted", "membership_application", application.id,
                                f"Organization #{organization.id}; plan #{plan.id}")
                db.session.commit()
                if requires_confirmation:
                    sent = issue_email_verification(member)
                    return render_template("email_pending.html", sent=sent, preview=email_preview_mode(), email=member.email)
                if g.user is None:
                    establish_session(member)
                flash(f"Your application to {organization.name} is with the organization for review.", "success")
                return redirect(url_for("member_home", selected_membership_id=membership.id))
            if requires_confirmation:
                db.session.commit()
                sent = issue_email_verification(member)
                return render_template("email_pending.html", sent=sent, preview=email_preview_mode(), email=member.email)
            payment = create_simulated_payment(membership, plan, "join", plan.price_minor)
            if g.user is None:
                establish_session(member)
            return redirect(url_for("payment_review", payment_id=payment.id))
        try:
            selected_plan_id = int(request.args.get("plan_id", "0"))
        except ValueError:
            selected_plan_id = 0
        return render_template("join.html", organization=organization, plans=plans,
                               selected_plan_id=selected_plan_id)

    @app.get("/member")
    @app.get("/member/<int:selected_membership_id>")
    @role_required("member")
    def member_home(selected_membership_id: int | None = None):
        memberships = Membership.query.filter_by(user_id=g.user.id).order_by(Membership.id.desc()).all()
        if selected_membership_id is not None:
            membership = next((item for item in memberships if item.id == selected_membership_id), None)
            if membership is None:
                abort(404)
        else:
            membership = memberships[0] if memberships else None
        if not membership:
            organizations = Organization.query.filter_by(status="Approved").order_by(Organization.name).all()
            return render_template("member.html", membership=None, memberships=memberships,
                                   organizations=organizations, application=None)
        status = membership_status(membership)
        application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
        payments = SimulatedPayment.query.filter_by(membership_id=membership.id).order_by(SimulatedPayment.id.desc()).all()
        pending_payment = next((p for p in payments if p.status == "Pending"), None)
        today = simulated_day()
        upgrades = []
        downgrades = []
        if status == "Active":
            left = max(0, membership.end_day - today)
            current_period = period_days(membership.plan)
            upgrades = [
                (plan, round((plan.price_minor - membership.plan.price_minor) * left / current_period))
                for plan in Plan.query.filter_by(organization_id=membership.organization_id, archived=False)
                .filter(Plan.billing_period == membership.plan.billing_period,
                        Plan.price_minor > membership.plan.price_minor).order_by(Plan.price_minor).all()
            ]
            downgrades = Plan.query.filter_by(organization_id=membership.organization_id, archived=False)
            downgrades = downgrades.filter(Plan.billing_period == membership.plan.billing_period,
                                           Plan.price_minor < membership.plan.price_minor).order_by(Plan.price_minor.desc()).all()
        return render_template("member.html", membership=membership, memberships=memberships,
                               status=status, payments=payments,
                               application=application,
                               pending_payment=pending_payment, upgrades=upgrades,
                               downgrades=downgrades,
                               days_left=max(0, (membership.end_day or today) - today),
                               renewal_reminder=(status == "Active" and not membership.cancel_at_period_end
                                                 and 0 < (membership.end_day or today) - today <= 7))

    @app.post("/member/<int:membership_id>/retry")
    @role_required("member")
    def member_retry_payment(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        status = membership_status(membership)
        if status not in ("Pending", "Expired"):
            abort(400)
        if status == "Pending" and membership.organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if not application or application.status != "Approved":
                flash("This organization must approve your membership application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=membership.id))
        plan = db.session.get(Plan, membership.plan_id)
        if plan.archived:
            flash("This plan is no longer available. Contact the organization before renewing.", "error")
            return redirect(url_for("member_home", selected_membership_id=membership.id))
        kind = "renewal" if status == "Expired" else "join"
        payment = create_simulated_payment(membership, plan, kind, plan.price_minor)
        return redirect(url_for("payment_review", payment_id=payment.id))

    @app.post("/member/<int:membership_id>/upgrade/<int:plan_id>")
    @role_required("member")
    def member_upgrade(membership_id: int, plan_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) != "Active":
            abort(400)
        target = Plan.query.filter_by(id=plan_id, organization_id=membership.organization_id, archived=False).first_or_404()
        current = membership.plan
        if target.price_minor <= current.price_minor or target.billing_period != current.billing_period:
            abort(400)
        days_left = max(0, membership.end_day - simulated_day())
        amount = round((target.price_minor - current.price_minor) * days_left / period_days(current))
        payment = create_simulated_payment(membership, target, "upgrade", amount)
        return redirect(url_for("payment_review", payment_id=payment.id))

    @app.post("/member/<int:membership_id>/cancel")
    @role_required("member")
    def member_cancel(membership_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) not in ("Active", "Cancelled"):
            abort(400)
        membership.cancel_at_period_end = not membership.cancel_at_period_end
        if membership.cancel_at_period_end:
            membership.next_plan_id = None
        record_activity("membership_cancellation_changed", "membership", membership.id,
                        "Cancellation at period end" if membership.cancel_at_period_end else "Cancellation undone")
        db.session.commit()
        flash("Your membership will remain active through the paid period." if membership.cancel_at_period_end
              else "Membership cancellation undone.", "success")
        return redirect(url_for("member_home", selected_membership_id=membership.id))

    @app.post("/member/<int:membership_id>/downgrade/<int:plan_id>")
    @role_required("member")
    def member_schedule_downgrade(membership_id: int, plan_id: int):
        membership = Membership.query.filter_by(id=membership_id, user_id=g.user.id).first_or_404()
        if membership_status(membership) != "Active" or membership.cancel_at_period_end:
            abort(400)
        target = Plan.query.filter_by(id=plan_id, organization_id=membership.organization_id,
                                     archived=False).first_or_404()
        if target.billing_period != membership.plan.billing_period or target.price_minor >= membership.plan.price_minor:
            abort(400)
        if membership.next_plan_id == target.id:
            membership.next_plan_id = None
            details = "Scheduled downgrade removed"
            message = "Scheduled plan change removed. Your current plan will continue."
        else:
            membership.next_plan_id = target.id
            details = f"Downgrade to plan {target.id} at period end"
            message = f"{target.name} is scheduled to start at the end of your current paid period."
        record_activity("membership_downgrade_scheduled", "membership", membership.id, details)
        db.session.commit()
        flash(message, "success")
        return redirect(url_for("member_home", selected_membership_id=membership.id))

    @app.route("/payments/<int:payment_id>/review", methods=["GET", "POST"])
    @role_required("member")
    def payment_review(payment_id: int):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment or payment.membership.user_id != g.user.id:
            abort(404)
        if payment.status != "Pending":
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))

        membership = payment.membership
        if (payment.kind == "join" and membership.organization.membership_approval_required):
            application = MembershipApplication.query.filter_by(membership_id=membership.id).first()
            if not application or application.status != "Approved":
                flash("This organization must approve your application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=membership.id))
        if request.method == "POST":
            if request.form.get("confirm_terms") != "yes":
                flash("Confirm the simulated payment and cancellation terms before continuing.", "error")
                return redirect(url_for("payment_review", payment_id=payment.id))
            # Only this server-rendered review can authorize the matching pending payment page.
            session["reviewed_payment_id"] = payment.id
            return redirect(url_for("simulated_bank", payment_id=payment.id))

        current_day = simulated_day()
        if payment.kind == "upgrade":
            starts_when = "Immediately after successful confirmation"
            ending = f"Existing paid period end, simulated day {membership.end_day}"
        else:
            starts_when = f"After successful confirmation, estimated simulated day {current_day}"
            ending = f"Estimated simulated day {current_day + period_days(payment.plan)}"
        return render_template("payment_review.html", payment=payment, membership=membership,
                               starts_when=starts_when, ending=ending)

    @app.get("/simulated-bank/<int:payment_id>")
    @role_required("member")
    def simulated_bank(payment_id: int):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment or payment.membership.user_id != g.user.id:
            abort(404)
        if payment.status != "Pending":
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if payment.kind == "join" and payment.organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=payment.membership_id).first()
            if not application or application.status != "Approved":
                session.pop("reviewed_payment_id", None)
                flash("This organization must approve your application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if session.get("reviewed_payment_id") != payment.id:
            return redirect(url_for("payment_review", payment_id=payment.id))
        return render_template("simulated_bank.html", payment=payment)

    @app.post("/simulated-bank/<int:payment_id>/<action>")
    @role_required("member")
    def simulated_bank_action(payment_id: int, action: str):
        payment = db.session.get(SimulatedPayment, payment_id)
        if not payment or payment.membership.user_id != g.user.id:
            abort(404)
        if payment.status != "Pending":
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if payment.kind == "join" and payment.organization.membership_approval_required:
            application = MembershipApplication.query.filter_by(membership_id=payment.membership_id).first()
            if not application or application.status != "Approved":
                session.pop("reviewed_payment_id", None)
                flash("This organization must approve your application before payment.", "info")
                return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if session.get("reviewed_payment_id") != payment.id:
            return redirect(url_for("payment_review", payment_id=payment.id))
        if action == "success":
            session.pop("reviewed_payment_id", None)
            settle_simulated_payment(payment)
            flash("Simulated payment succeeded. No money was collected.", "success")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if action == "duplicate":
            session.pop("reviewed_payment_id", None)
            settle_simulated_payment(payment)
            settle_simulated_payment(payment)
            flash("Simulated payment succeeded. Duplicate confirmation was safely ignored.", "success")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if action == "late":
            session.pop("reviewed_payment_id", None)
            record_activity("simulated_payment_late_confirmation", "payment", payment.id,
                            "Awaiting platform admin confirmation")
            db.session.commit()
            flash("Payment is awaiting delayed confirmation. An admin can deliver it from the platform dashboard.", "info")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        if action in ("failure", "abandon"):
            session.pop("reviewed_payment_id", None)
            payment.status = "Failed" if action == "failure" else "Abandoned"
            record_activity(f"simulated_payment_{payment.status.lower()}", "payment", payment.id,
                            "Simulated bank outcome")
            db.session.commit()
            flash("The simulated payment did not complete. You can retry from your membership page.", "error")
            return redirect(url_for("member_home", selected_membership_id=payment.membership_id))
        abort(404)

    @app.cli.command("create-admin")
    def create_admin():
        """Create the first platform administrator interactively."""
        email = click.prompt("Platform administrator email").strip().lower()
        password = click.prompt("Password (12+ characters)", hide_input=True, confirmation_prompt=True)
        if not valid_email(email) or len(password) < 12 or len(password) > 128 or User.query.filter_by(email=email, role="platform_admin").first():
            raise click.ClickException("Password length is invalid or that email already exists.")
        db.session.add(User(email=email, password_hash=password_hasher.hash(password), role="platform_admin",
                            email_verified=True))
        db.session.commit()
        click.echo("Platform administrator created.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create sample organization data and an interactively configured local admin."""
        public_url = urlsplit(app.config.get("PUBLIC_BASE_URL", ""))
        if db.engine.dialect.name != "sqlite" or public_url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise click.ClickException("Sample data can only be created in a local SQLite database.")

        demo_email = "chess@example.com"
        existing = Organization.query.filter_by(email=demo_email).first()
        if existing:
            if existing.name != "Colombo Chess Club":
                raise click.ClickException("The demo contact email is already used by a different organization.")
            click.echo("Colombo Chess Club sample data already exists; nothing was changed.")
            return

        admin_email = click.prompt("Local demo organization administrator email").strip().lower()
        password = click.prompt("Password for the demo organization admin (12+ characters)",
                                hide_input=True, confirmation_prompt=True)
        if not valid_email(admin_email) or len(password) < 12 or len(password) > 128:
            raise click.ClickException("Enter a valid email and a password between 12 and 128 characters.")

        organization = Organization(name="Colombo Chess Club", email=demo_email, status="Approved")
        db.session.add(organization)
        db.session.flush()
        administrator = User(email=admin_email, full_name="Club Administrator",
                             password_hash=password_hasher.hash(password), role="organization_admin",
                             organization_id=organization.id, email_verified=True)
        community = Plan(organization_id=organization.id, name="Community", price_minor=150000,
                         billing_period="monthly",
                         benefits="Weekly club nights\nMember discussion group\nCasual tournament entry")
        supporter = Plan(organization_id=organization.id, name="Club Supporter", price_minor=300000,
                         billing_period="monthly",
                         benefits="Everything in Community\nPriority tournament registration\nOne guest pass each month")
        annual = Plan(organization_id=organization.id, name="Annual Player", price_minor=1500000,
                      billing_period="yearly",
                      benefits="Everything in Community\nAnnual club championship entry\nTwo guest passes")
        db.session.add_all([administrator, community, supporter, annual])
        db.session.flush()
        db.session.add_all([
            OrganizationOffer(organization_id=organization.id, plan_id=community.id,
                              title="First club night is on us",
                              description="Meet the players and join one weekly club night as our guest.",
                              offer_type="Welcome bonus", start_day=simulated_day()),
            OrganizationOffer(organization_id=organization.id, plan_id=None,
                              title="Bring a friend",
                              description="Invite a friend to your first casual tournament.",
                              offer_type="Extra benefit", start_day=simulated_day()),
        ])
        record_activity("local_demo_seeded", "organization", organization.id,
                        "Sample organization, plans, offers, and organization admin created")
        db.session.commit()
        click.echo("Local sample data is ready: Colombo Chess Club, three plans, two offers, and its organization admin.")

    @app.cli.command("init-db")
    def init_db():
        """Create local tables for the initial prototype."""
        ensure_local_sqlite_schema()
        click.echo("Database tables created. Use migrations for later schema changes.")

    return app
