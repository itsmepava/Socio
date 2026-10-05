from conftest import login_as

from app import ActivityLog, Organization, Plan, User, db


def test_requires_secret_key():
    from app import create_app
    import pytest
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"SECRET_KEY": None})


def test_login_and_session_cookie_security(client):
    response = client.post("/login", data={"email": "a-admin@example.test", "password": "correct horse battery"})
    assert response.status_code == 302
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_logout_revokes_existing_session(app, client):
    client.post("/login", data={"email":"a-admin@example.test", "password":"correct horse battery"})
    with client.session_transaction() as session:
        stolen_values = dict(session)
    assert client.post("/logout").status_code == 302
    replay = app.test_client()
    with replay.session_transaction() as session:
        session.update(stolen_values)
    assert replay.get("/organization/plans").status_code == 302


def test_bad_login_does_not_disclose_account(client):
    a = client.post("/login", data={"email": "missing@example.test", "password": "wrong"})
    b = client.post("/login", data={"email": "a-admin@example.test", "password": "wrong"})
    assert b"Email or password is incorrect." in a.data
    assert b"Email or password is incorrect." in b.data


def test_duplicate_member_email_is_scoped_by_organization(app, client):
    with app.app_context():
        org_b = Organization.query.filter_by(email="b@example.test").one()
        db.session.add(User(email="member@example.test", password_hash=User.query.filter_by(email="member@example.test").one().password_hash,
                            role="member", organization_id=org_b.id))
        db.session.commit()
    ambiguous = client.post("/login", data={"email":"member@example.test", "password":"correct horse battery"})
    assert b"Email or password is incorrect." in ambiguous.data
    selected = client.post("/login", data={"email":"member@example.test", "organization_email":"b@example.test", "password":"correct horse battery"})
    assert selected.status_code == 302
    with client.session_transaction() as session:
        member_id = session["user_id"]
    with app.app_context():
        assert db.session.get(User, member_id).organization.email == "b@example.test"


def test_member_cannot_open_platform_admin(client):
    login_as(client, "member@example.test")
    assert client.get("/admin/organizations").status_code == 403


def test_organization_admin_cannot_open_platform_admin(client):
    login_as(client, "a-admin@example.test")
    assert client.get("/admin/organizations").status_code == 403


def test_platform_admin_can_suspend_and_action_is_logged(app, client):
    login_as(client, "platform@example.test")
    response = client.post("/admin/organizations/1/suspend")
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Organization, 1).status == "Suspended"
        log = ActivityLog.query.filter_by(action="organization_suspend").one()
        assert log.actor_user_id == User.query.filter_by(email="platform@example.test").one().id


def test_platform_admin_can_approve_pending_organization(app, client):
    with app.app_context():
        org = Organization(name="Pending Society", email="awaiting@example.test", status="Pending")
        db.session.add(org)
        db.session.commit()
        org_id = org.id
    login_as(client, "platform@example.test")
    assert client.post(f"/admin/organizations/{org_id}/approve").status_code == 302
    with app.app_context():
        assert db.session.get(Organization, org_id).status == "Approved"


def test_invalid_organization_status_transition_is_rejected(app, client):
    login_as(client, "platform@example.test")
    assert client.post("/admin/organizations/1/reinstate").status_code == 409
    with app.app_context():
        assert db.session.get(Organization, 1).status == "Approved"


def test_organization_admin_sees_only_own_plans(client):
    login_as(client, "a-admin@example.test")
    response = client.get("/organization/plans")
    assert response.status_code == 200
    assert b"B Plan" not in response.data


def test_organization_admin_cannot_archive_another_organizations_plan(client):
    login_as(client, "a-admin@example.test")
    assert client.post("/organization/plans/1/archive").status_code == 404


def test_unapproved_organization_cannot_manage_plans(app, client):
    with app.app_context():
        org = Organization(name="Pending Society", email="pending@example.test", status="Pending")
        db.session.add(org)
        db.session.flush()
        user = User(email="pending-admin@example.test", password_hash="x", role="organization_admin", organization_id=org.id)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    login_as(client, "pending-admin@example.test")
    assert client.get("/organization/plans").status_code == 200
    assert client.post("/organization/plans", data={"name":"Blocked","price_minor":"100","billing_period":"monthly"}).status_code == 403


def test_plan_input_rejects_invalid_amount_and_period(client):
    login_as(client, "a-admin@example.test")
    response = client.post("/organization/plans", data={"name":"Plan", "price_minor":"1.5", "billing_period":"monthly"})
    assert response.status_code == 200
    assert b"Price must be a non-negative whole number" in response.data
    response = client.post("/organization/plans", data={"name":"Plan", "price_minor":"100", "billing_period":"weekly"})
    assert b"valid plan name" in response.data


def test_csrf_rejects_state_change_without_token(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    login_as(client, "platform@example.test")
    assert client.post("/admin/organizations/1/approve").status_code == 400


def test_archiving_keeps_record_and_marks_it_unavailable(app, client):
    login_as(client, "a-admin@example.test")
    client.post("/organization/plans", data={"name":"Annual", "price_minor":"10000", "billing_period":"yearly"})
    with app.app_context():
        plan = Plan.query.filter_by(name="Annual").one()
        plan_id = plan.id
    client.post(f"/organization/plans/{plan_id}/archive")
    with app.app_context():
        assert db.session.get(Plan, plan_id).archived is True


def test_registration_hashes_password_and_logs_event(app, client):
    response = client.post("/organizations/register", data={
        "name":"New Society", "organization_email":"new-org@example.test",
        "email":"new-admin@example.test", "password":"a very long test password",
    })
    assert response.status_code == 302
    with app.app_context():
        user = User.query.filter_by(email="new-admin@example.test").one()
        assert user.password_hash != "a very long test password"
        assert user.role == "organization_admin"
        assert ActivityLog.query.filter_by(action="organization_registered").count() == 1
