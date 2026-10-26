"""Projects: validation, who may create them, membership isolation, and incident matching."""

import pytest

from backend.db.audit_models import AuditEvent
from backend.ui.projects import job_matches

GOOD = {"name": "Shop", "jenkins_job": "target-app", "github_repo": "acme/shop"}


def test_a_reviewer_registers_a_project_and_becomes_a_member(sign_in, world):
    world.user("rev@acme.io", "reviewer")
    rev = sign_in("rev@acme.io")
    created = rev.post("/api/v1/projects", json={**GOOD, "description": " Orders service ", "prometheus_job": "target_app"})
    assert created.status_code == 201
    body = created.json()
    assert (body["name"], body["description"], body["prometheus_job"], body["incident_count"]) == ("Shop", "Orders service", "target_app", 0)
    assert [p["name"] for p in rev.get("/api/v1/projects").json()] == ["Shop"]


def test_a_viewer_cannot_create_a_project(sign_in, world):
    world.user("view@acme.io", "viewer")
    assert sign_in("view@acme.io").post("/api/v1/projects", json=GOOD).status_code == 403


@pytest.mark.parametrize(
    "bad",
    [
        {"github_repo": "https://github.com/acme/shop"},
        {"github_repo": "acme"},
        {"github_repo": "acme/shop; rm -rf /"},
        {"jenkins_job": "a//b"},
        {"jenkins_job": "../escape"},
        {"jenkins_job": "a" * 300},
        {"prometheus_job": "bad label!"},
        {"name": "x"},
    ],
)
def test_project_fields_are_validated(sign_in, world, bad):
    world.user("rev@acme.io", "reviewer")
    assert sign_in("rev@acme.io").post("/api/v1/projects", json={**GOOD, **bad}).status_code == 422


def test_unknown_fields_are_refused(sign_in, world):
    world.user("rev@acme.io", "reviewer")
    assert sign_in("rev@acme.io").post("/api/v1/projects", json={**GOOD, "role": "admin"}).status_code == 422


def test_a_jenkins_job_or_name_can_only_be_registered_once(sign_in, world):
    world.user("rev@acme.io", "reviewer")
    rev = sign_in("rev@acme.io")
    assert rev.post("/api/v1/projects", json=GOOD).status_code == 201
    assert rev.post("/api/v1/projects", json={**GOOD, "name": "Other"}).status_code == 409  # same job
    assert rev.post("/api/v1/projects", json={**GOOD, "jenkins_job": "other-job"}).status_code == 409  # same name


def test_a_project_is_invisible_to_people_who_are_not_members(sign_in, world):
    owner = world.user("owner@acme.io", "reviewer")
    world.user("other@acme.io", "reviewer")
    admin = world.user("admin@acme.io", "admin")
    project_id = world.project("Shop", "target-app", [owner])
    other, boss = sign_in("other@acme.io"), sign_in("admin@acme.io")
    assert other.get("/api/v1/projects").json() == []
    assert other.get(f"/api/v1/projects/{project_id}").status_code == 404  # 404, not 403: existence is not revealed
    assert other.get(f"/api/v1/projects/{project_id}/incidents").status_code == 404
    assert other.get(f"/api/v1/projects/{project_id}/stats").status_code == 404
    assert other.get(f"/api/v1/projects/{project_id}/reviews").status_code == 404
    assert [p["name"] for p in boss.get("/api/v1/projects").json()] == ["Shop"]
    assert admin


def test_job_matching_means_the_job_itself_or_anything_under_it():
    assert job_matches("target-app", "target-app")
    assert job_matches("target-app", "target-app/main")
    assert job_matches("team/target-app", "team/target-app/feature-x")
    assert not job_matches("target-app", "target-app-2")
    assert not job_matches("target-app", "my-target-app/main")


def test_a_project_counts_only_its_own_incidents(sign_in, world):
    user = world.user("rev@acme.io", "reviewer")
    project_id = world.project("Shop", "target-app", [user])
    world.incident("target-app/main")
    world.incident("target-app/dev", status="RESOLVED")
    world.incident("target-app-2/main")  # a different job that merely shares a prefix
    world.incident("% wildcard/main")
    summary = sign_in("rev@acme.io").get(f"/api/v1/projects/{project_id}").json()
    assert (summary["incident_count"], summary["open_incidents"]) == (2, 1)


def test_like_wildcards_in_a_job_name_do_not_match_other_jobs(sign_in, world):
    user = world.user("rev@acme.io", "reviewer")
    project_id = world.project("Odd", "100%_job", [user])
    world.incident("100%_job/main")
    world.incident("100xxjob/main")
    assert sign_in("rev@acme.io").get(f"/api/v1/projects/{project_id}").json()["incident_count"] == 1


def test_only_the_creator_or_an_admin_archives_a_project(sign_in, world):
    creator = world.user("creator@acme.io", "reviewer")
    peer = world.user("peer@acme.io", "reviewer")
    project_id = world.project("Shop", "target-app", [creator, peer])
    with_peer = sign_in("peer@acme.io").patch(f"/api/v1/projects/{project_id}", json={"archived": True})
    assert with_peer.status_code == 403  # created_by is unset for fixtures, so only an admin may archive
    world.user("admin@acme.io", "admin")
    assert sign_in("admin@acme.io").patch(f"/api/v1/projects/{project_id}", json={"archived": True}).status_code == 200
    assert sign_in("creator@acme.io").get("/api/v1/projects").json() == []  # archived projects drop out of the list


def test_an_admin_sets_the_members_of_a_project(sign_in, world):
    world.user("admin@acme.io", "admin")
    ana = world.user("ana@acme.io", "viewer")
    project_id = world.project("Shop", "target-app")
    admin = sign_in("admin@acme.io")
    assert admin.put(f"/api/v1/projects/{project_id}/members", json={"user_ids": [ana]}).status_code == 200
    assert [p["name"] for p in sign_in("ana@acme.io").get("/api/v1/projects").json()] == ["Shop"]
    assert admin.put(f"/api/v1/projects/{project_id}/members", json={"user_ids": []}).json() == []
    assert sign_in("ana@acme.io").get("/api/v1/projects").json() == []
    assert admin.put(f"/api/v1/projects/{project_id}/members", json={"user_ids": [9999]}).status_code == 422


def test_non_admins_cannot_read_or_change_members(sign_in, world):
    user = world.user("rev@acme.io", "reviewer")
    project_id = world.project("Shop", "target-app", [user])
    rev = sign_in("rev@acme.io")
    assert rev.get(f"/api/v1/projects/{project_id}/members").status_code == 403
    assert rev.put(f"/api/v1/projects/{project_id}/members", json={"user_ids": [user]}).status_code == 403


def test_project_changes_are_audited(sign_in, world, database):
    from sqlalchemy.orm import Session

    world.user("rev@acme.io", "reviewer")
    sign_in("rev@acme.io").post("/api/v1/projects", json=GOOD)
    with Session(database) as session:
        events = session.query(AuditEvent).filter(AuditEvent.event_type == "project_created").all()
    assert len(events) == 1 and events[0].actor == "rev@acme.io" and events[0].payload["github_repo"] == "acme/shop"
