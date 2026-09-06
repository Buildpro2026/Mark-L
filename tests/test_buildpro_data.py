import pytest

from actions import buildpro_data as bd


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_buildpro.db")


# ── candidates ───────────────────────────────────────────────────────────

def test_add_and_get_candidate():
    cid = bd.add_candidate("Jane Doe", email="jane@example.com", phone="+15551234567", source="hubspot")
    row = bd.get_candidate(cid)
    assert row["name"] == "Jane Doe"
    assert row["email"] == "jane@example.com"
    assert row["status"] == "new"
    assert row["created_ts"] == row["updated_ts"]


def test_add_candidate_rejects_unknown_status():
    with pytest.raises(ValueError):
        bd.add_candidate("Bad Status", status="not_a_real_status")


def test_list_candidates_orders_newest_updated_first():
    c1 = bd.add_candidate("First")
    c2 = bd.add_candidate("Second")
    bd.update_candidate(c1, notes="touched again")
    rows = bd.list_candidates()
    assert rows[0]["id"] == c1
    assert rows[1]["id"] == c2


def test_list_candidates_filters_by_status():
    bd.add_candidate("A", status="new")
    b = bd.add_candidate("B", status="placed")
    rows = bd.list_candidates(status="placed")
    assert len(rows) == 1
    assert rows[0]["id"] == b


def test_update_candidate_changes_fields_and_updated_ts():
    cid = bd.add_candidate("Original")
    before = bd.get_candidate(cid)["updated_ts"]
    ok = bd.update_candidate(cid, status="screening", notes="phone screen done")
    assert ok is True
    row = bd.get_candidate(cid)
    assert row["status"] == "screening"
    assert row["notes"] == "phone screen done"
    assert row["updated_ts"] >= before


def test_update_candidate_rejects_unknown_status():
    cid = bd.add_candidate("X")
    with pytest.raises(ValueError):
        bd.update_candidate(cid, status="bogus")


def test_update_candidate_with_no_fields_returns_false():
    cid = bd.add_candidate("X")
    assert bd.update_candidate(cid) is False


def test_get_candidate_missing_returns_none():
    assert bd.get_candidate(99999) is None


# ── candidate duplicate handling (upsert_candidate) ───────────────────────

def test_find_candidate_by_email_is_case_insensitive():
    cid = bd.add_candidate("Jane Doe", email="Jane@Example.com")
    found = bd.find_candidate_by_email("jane@example.com")
    assert found["id"] == cid


def test_find_candidate_by_email_no_match_returns_none():
    assert bd.find_candidate_by_email("nobody@example.com") is None


def test_find_candidate_by_email_empty_string_returns_none():
    bd.add_candidate("Jane Doe", email="")
    assert bd.find_candidate_by_email("") is None


def test_upsert_candidate_creates_when_no_existing_match():
    cid, action = bd.upsert_candidate("Jane Doe", email="jane@example.com", title="Electrician")
    assert action == "created"
    assert bd.get_candidate(cid)["title"] == "Electrician"


def test_upsert_candidate_called_twice_never_creates_a_duplicate():
    first_id, first_action = bd.upsert_candidate("Jane Doe", email="jane@example.com", title="Electrician")
    second_id, second_action = bd.upsert_candidate("Jane Doe", email="jane@example.com", title="Senior Electrician")

    assert first_action == "created"
    assert second_action == "updated"
    assert first_id == second_id
    assert len(bd.list_candidates()) == 1
    assert bd.get_candidate(first_id)["title"] == "Senior Electrician"


def test_upsert_candidate_without_email_always_creates_new_rows():
    # No dedup key available — matches add_candidate()'s existing behavior.
    id1, action1 = bd.upsert_candidate("Anonymous Candidate")
    id2, action2 = bd.upsert_candidate("Anonymous Candidate")
    assert action1 == action2 == "created"
    assert id1 != id2
    assert len(bd.list_candidates()) == 2


def test_upsert_candidate_update_does_not_blank_out_unspecified_fields():
    cid, _ = bd.upsert_candidate("Jane Doe", email="jane@example.com", title="Electrician", location="Dallas")
    bd.upsert_candidate("Jane Doe", email="jane@example.com", title="Senior Electrician")
    row = bd.get_candidate(cid)
    assert row["title"] == "Senior Electrician"
    assert row["location"] == "Dallas"   # untouched by the second call


# ── clients ──────────────────────────────────────────────────────────────

def test_add_and_get_client():
    cid = bd.add_client("Acme Construction", contact_name="Bob", email="bob@acme.com")
    row = bd.get_client(cid)
    assert row["name"] == "Acme Construction"
    assert row["status"] == "active"


def test_add_client_rejects_unknown_status():
    with pytest.raises(ValueError):
        bd.add_client("Bad", status="deleted")


def test_list_clients_filters_by_status():
    bd.add_client("Prospect Co", status="prospect")
    active_id = bd.add_client("Active Co", status="active")
    rows = bd.list_clients(status="active")
    assert len(rows) == 1
    assert rows[0]["id"] == active_id


def test_update_client():
    cid = bd.add_client("Renaming Co")
    bd.update_client(cid, name="Renamed Co", status="inactive")
    row = bd.get_client(cid)
    assert row["name"] == "Renamed Co"
    assert row["status"] == "inactive"


# ── client duplicate handling (upsert_client) ─────────────────────────────

def test_find_client_by_email_is_case_insensitive():
    cid = bd.add_client("Acme Construction", email="Bob@Acme.com")
    found = bd.find_client_by_email("bob@acme.com")
    assert found["id"] == cid


def test_upsert_client_called_twice_never_creates_a_duplicate():
    first_id, first_action = bd.upsert_client("Acme Construction", email="bob@acme.com", industry="construction")
    second_id, second_action = bd.upsert_client("Acme Construction", email="bob@acme.com", industry="general contracting")

    assert first_action == "created"
    assert second_action == "updated"
    assert first_id == second_id
    assert len(bd.list_clients()) == 1
    assert bd.get_client(first_id)["industry"] == "general contracting"


def test_upsert_client_without_email_always_creates_new_rows():
    id1, _ = bd.upsert_client("Some Company")
    id2, _ = bd.upsert_client("Some Company")
    assert id1 != id2


# ── jobs ─────────────────────────────────────────────────────────────────

def test_add_and_get_job_linked_to_client():
    client_id = bd.add_client("BuilderCo")
    job_id = bd.add_job("Site Superintendent", client_id=client_id, location="Houston, TX")
    row = bd.get_job(job_id)
    assert row["title"] == "Site Superintendent"
    assert row["client_id"] == client_id
    assert row["status"] == "open"


def test_add_job_without_client_is_allowed():
    job_id = bd.add_job("Unassigned Role")
    row = bd.get_job(job_id)
    assert row["client_id"] is None


def test_add_job_rejects_unknown_status():
    with pytest.raises(ValueError):
        bd.add_job("X", status="cancelled")


def test_list_jobs_filters_by_status_and_client():
    c1 = bd.add_client("Client One")
    c2 = bd.add_client("Client Two")
    bd.add_job("Open Job", client_id=c1, status="open")
    bd.add_job("Filled Job", client_id=c1, status="filled")
    bd.add_job("Other Client Job", client_id=c2, status="open")

    open_at_c1 = bd.list_jobs(status="open", client_id=c1)
    assert len(open_at_c1) == 1
    assert open_at_c1[0]["title"] == "Open Job"


def test_update_job_status():
    job_id = bd.add_job("Foreman")
    bd.update_job(job_id, status="filled")
    assert bd.get_job(job_id)["status"] == "filled"


# ── matches ──────────────────────────────────────────────────────────────

def test_add_and_get_match():
    cand = bd.add_candidate("Match Candidate")
    job = bd.add_job("Match Job")
    match_id = bd.add_match(cand, job, match_score=85.5, match_rationale="10 years relevant experience")
    row = bd.get_match(match_id)
    assert row["candidate_id"] == cand
    assert row["job_id"] == job
    assert row["match_score"] == 85.5
    assert row["status"] == "proposed"


def test_add_match_rejects_unknown_status():
    cand = bd.add_candidate("C")
    job = bd.add_job("J")
    with pytest.raises(ValueError):
        bd.add_match(cand, job, status="bogus")


def test_list_matches_orders_by_score_desc():
    cand = bd.add_candidate("C")
    job1 = bd.add_job("J1")
    job2 = bd.add_job("J2")
    bd.add_match(cand, job1, match_score=50)
    high = bd.add_match(cand, job2, match_score=95)
    rows = bd.list_matches(candidate_id=cand)
    assert rows[0]["id"] == high


def test_update_match_status():
    cand = bd.add_candidate("C")
    job = bd.add_job("J")
    match_id = bd.add_match(cand, job, match_score=60)
    bd.update_match(match_id, status="submitted")
    assert bd.get_match(match_id)["status"] == "submitted"


# ── summary (Command Center feed) ───────────────────────────────────────

def test_summary_counts_are_accurate():
    bd.add_candidate("C1")
    bd.add_candidate("C2")
    bd.add_client("Client1")
    bd.add_job("Open Job", status="open")
    bd.add_job("Closed Job", status="closed")

    s = bd.summary()
    assert s["candidate_count"] == 2
    assert s["client_count"] == 1
    assert s["active_jobs"] == 1


def test_summary_qualified_matches_uses_threshold():
    cand = bd.add_candidate("C")
    job = bd.add_job("J")
    bd.add_match(cand, job, match_score=40)   # below threshold
    bd.add_match(cand, job, match_score=90)   # above threshold

    s = bd.summary()
    assert s["qualified_matches"] == 1
    assert s["qualified_match_threshold"] == bd.QUALIFIED_MATCH_SCORE


def test_summary_highest_match_scores_includes_names():
    cand = bd.add_candidate("Top Candidate")
    job = bd.add_job("Top Job")
    bd.add_match(cand, job, match_score=99)

    s = bd.summary()
    assert len(s["highest_match_scores"]) == 1
    top = s["highest_match_scores"][0]
    assert top["candidate_name"] == "Top Candidate"
    assert top["job_title"] == "Top Job"
    assert top["match_score"] == 99


def test_summary_recent_activity_includes_all_three_entities():
    bd.add_candidate("Recent Candidate")
    bd.add_client("Recent Client")
    bd.add_job("Recent Job")

    s = bd.summary()
    assert len(s["recent_activity"]["candidates"]) == 1
    assert len(s["recent_activity"]["clients"]) == 1
    assert len(s["recent_activity"]["jobs"]) == 1
    assert s["recent_activity"]["candidates"][0]["label"] == "Recent Candidate"


def test_summary_on_empty_database():
    s = bd.summary()
    assert s["candidate_count"] == 0
    assert s["client_count"] == 0
    assert s["active_jobs"] == 0
    assert s["qualified_matches"] == 0
    assert s["highest_match_scores"] == []


# ── recruiting-engine schema extensions (matching fields, sync, lookups) ──

def test_add_candidate_with_matching_fields():
    cid = bd.add_candidate(
        "Match Ready", title="Electrician", specialty="electrical", years_experience=7,
        skills="osha-30, blueprint reading", location="Houston, TX",
        desired_compensation="$75,000", availability="available",
    )
    row = bd.get_candidate(cid)
    assert row["title"] == "Electrician"
    assert row["years_experience"] == 7
    assert row["availability"] == "available"


def test_add_candidate_rejects_unknown_availability():
    with pytest.raises(ValueError):
        bd.add_candidate("X", availability="on_vacation")


def test_get_candidate_by_hubspot_id():
    bd.add_candidate("Synced Candidate", hubspot_contact_id="hs-123")
    row = bd.get_candidate_by_hubspot_id("hs-123")
    assert row is not None
    assert row["name"] == "Synced Candidate"
    assert bd.get_candidate_by_hubspot_id("does-not-exist") is None


def test_get_client_by_hubspot_id():
    bd.add_client("Synced Client", hubspot_company_id="hs-co-1")
    row = bd.get_client_by_hubspot_id("hs-co-1")
    assert row is not None
    assert row["name"] == "Synced Client"
    assert bd.get_client_by_hubspot_id("nope") is None


def test_find_candidates_by_skills():
    bd.add_candidate("Welder", skills="welding, blueprint reading")
    bd.add_candidate("Plumber", skills="plumbing, pipefitting")
    results = bd.find_candidates(skills="welding")
    assert len(results) == 1
    assert results[0]["name"] == "Welder"


def test_find_candidates_by_location_and_title():
    bd.add_candidate("Houston Electrician", title="Electrician", location="Houston, TX")
    bd.add_candidate("Dallas Electrician", title="Electrician", location="Dallas, TX")
    results = bd.find_candidates(location="Houston")
    assert len(results) == 1
    assert results[0]["name"] == "Houston Electrician"


def test_find_candidates_by_min_experience():
    bd.add_candidate("Junior", years_experience=2)
    bd.add_candidate("Senior", years_experience=10)
    results = bd.find_candidates(min_years_experience=5)
    assert len(results) == 1
    assert results[0]["name"] == "Senior"


def test_find_candidates_combines_filters_with_and():
    bd.add_candidate("Match", skills="welding", location="Houston, TX", status="new")
    bd.add_candidate("WrongLocation", skills="welding", location="Dallas, TX", status="new")
    results = bd.find_candidates(skills="welding", location="Houston")
    assert len(results) == 1
    assert results[0]["name"] == "Match"


def test_list_candidates_needing_followup_respects_stale_window():
    stale = bd.add_candidate("Stale Candidate", status="new")
    fresh = bd.add_candidate("Fresh Candidate", status="new")
    # Force the "stale" candidate's updated_ts far into the past directly.
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (time.time() - 30 * 86400, stale))
    conn.commit()
    conn.close()

    followups = bd.list_candidates_needing_followup(days=7)
    ids = {c["id"] for c in followups}
    assert stale in ids
    assert fresh not in ids


def test_list_candidates_needing_followup_excludes_closed_statuses():
    cid = bd.add_candidate("Placed Long Ago", status="new")
    bd.update_candidate(cid, status="placed")
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (time.time() - 30 * 86400, cid))
    conn.commit()
    conn.close()

    followups = bd.list_candidates_needing_followup(days=7)
    assert cid not in {c["id"] for c in followups}


def test_list_clients_needing_followup_respects_stale_window():
    stale = bd.add_client("Stale Client", status="prospect")
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_clients SET updated_ts = ? WHERE id = ?", (time.time() - 30 * 86400, stale))
    conn.commit()
    conn.close()

    followups = bd.list_clients_needing_followup(days=7)
    assert stale in {c["id"] for c in followups}


def test_list_unmatched_open_jobs():
    matched_job = bd.add_job("Has Match", status="open")
    unmatched_job = bd.add_job("No Match", status="open")
    closed_job = bd.add_job("Closed, No Match", status="closed")
    cand = bd.add_candidate("Candidate")
    bd.add_match(cand, matched_job, match_score=80)

    unmatched = bd.list_unmatched_open_jobs()
    ids = {j["id"] for j in unmatched}
    assert unmatched_job in ids
    assert matched_job not in ids
    assert closed_job not in ids  # only open jobs count


def test_top_matches_includes_candidate_and_job_names():
    cand = bd.add_candidate("Named Candidate")
    job = bd.add_job("Named Job")
    bd.add_match(cand, job, match_score=88)

    top = bd.top_matches(limit=5)
    assert len(top) == 1
    assert top[0]["candidate_name"] == "Named Candidate"
    assert top[0]["job_title"] == "Named Job"
    assert top[0]["match_score"] == 88


def test_summary_separates_clients_from_prospects():
    bd.add_client("Real Client", status="active")
    bd.add_client("A Prospect", status="prospect")
    s = bd.summary()
    assert s["client_count"] == 1
    assert s["prospect_count"] == 1


def test_summary_includes_followup_counts():
    stale = bd.add_candidate("Stale", status="new")
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (time.time() - 30 * 86400, stale))
    conn.commit()
    conn.close()
    s = bd.summary()
    assert s["candidates_needing_followup"] == 1


# ── sync run tracking ────────────────────────────────────────────────────

def test_record_and_get_last_sync():
    bd.record_sync_run("candidates", "hubspot", created_count=3, updated_count=2, error_count=0, errors=[])
    last = bd.get_last_sync("candidates")
    assert last["created_count"] == 3
    assert last["updated_count"] == 2
    assert last["errors"] == []


def test_get_last_sync_returns_none_when_no_runs_recorded():
    assert bd.get_last_sync("candidates") is None


def test_get_last_sync_returns_most_recent_run():
    bd.record_sync_run("candidates", "hubspot", created_count=1)
    bd.record_sync_run("candidates", "hubspot", created_count=5)
    last = bd.get_last_sync("candidates")
    assert last["created_count"] == 5


def test_record_sync_run_persists_errors():
    bd.record_sync_run("clients", "hubspot", error_count=2, errors=["bad record 1", "bad record 2"])
    last = bd.get_last_sync("clients")
    assert last["errors"] == ["bad record 1", "bad record 2"]


def test_list_sync_runs_orders_newest_first():
    bd.record_sync_run("candidates", "hubspot", created_count=1)
    bd.record_sync_run("candidates", "hubspot", created_count=2)
    runs = bd.list_sync_runs("candidates")
    assert runs[0]["created_count"] == 2
    assert runs[1]["created_count"] == 1


def test_split_keywords_normalizes_comma_list():
    assert bd.split_keywords("Welding, OSHA-30,  Crane Operation") == ["welding", "osha-30", "crane operation"]
    assert bd.split_keywords(None) == []
    assert bd.split_keywords("") == []
