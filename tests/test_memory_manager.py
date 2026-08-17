"""memory/memory_manager.py: schema defaults, round trips, atomic writes,
corruption recovery, and the read-modify-write race that used to exist
between load_memory() and save_memory() being separately locked.
"""
import threading

import pytest

import memory.memory_manager as mm


@pytest.fixture(autouse=True)
def _isolate_memory_path(tmp_path, monkeypatch):
    fake_path = tmp_path / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", fake_path)
    yield fake_path


# ── schema / round trips ────────────────────────────────────────────────

def test_load_memory_returns_empty_schema_when_file_missing():
    mem = mm.load_memory()
    assert mem == mm._empty_memory()
    assert set(mem.keys()) == {"identity", "preferences", "projects", "relationships", "wishes", "notes"}


def test_save_and_load_round_trip():
    mm.save_memory({"identity": {"name": {"value": "Lee", "updated": "2026-01-01"}}})
    mem = mm.load_memory()
    assert mem["identity"]["name"]["value"] == "Lee"


def test_load_memory_fills_in_missing_top_level_categories(_isolate_memory_path):
    _isolate_memory_path.parent.mkdir(parents=True, exist_ok=True)
    _isolate_memory_path.write_text('{"identity": {"name": {"value": "Lee"}}}', encoding="utf-8")
    mem = mm.load_memory()
    assert mem["identity"]["name"]["value"] == "Lee"
    assert mem["preferences"] == {}   # missing category backfilled to empty dict, not dropped


def test_update_memory_persists_and_merges():
    mm.update_memory({"preferences": {"favorite_color": {"value": "blue"}}})
    mm.update_memory({"preferences": {"favorite_food": {"value": "pizza"}}})
    mem = mm.load_memory()
    assert mem["preferences"]["favorite_color"]["value"] == "blue"
    assert mem["preferences"]["favorite_food"]["value"] == "pizza"


def test_remember_and_forget_round_trip():
    mm.remember("nickname", "Chief", category="identity")
    mem = mm.load_memory()
    assert mem["identity"]["nickname"]["value"] == "Chief"

    result = mm.forget("nickname", category="identity")
    assert "Forgotten" in result
    mem = mm.load_memory()
    assert "nickname" not in mem["identity"]


def test_forget_missing_key_reports_not_found():
    result = mm.forget("does_not_exist", category="notes")
    assert "Not found" in result


def test_save_session_summary_and_pop_last_session_round_trip():
    mm.save_session_summary("Discussed the BuildPro pipeline.", language="en")
    entry = mm.pop_last_session()
    assert entry is not None
    assert entry["summary"] == "Discussed the BuildPro pipeline."
    assert entry["language"] == "en"
    # consumed — a second pop finds nothing
    assert mm.pop_last_session() is None


def test_save_session_summary_caps_at_session_max():
    for i in range(mm._SESSION_MAX + 2):
        mm.save_session_summary(f"session {i}")
    mem = mm.load_memory()
    assert len(mem["sessions"]) == mm._SESSION_MAX
    assert mem["sessions"][-1]["summary"] == f"session {mm._SESSION_MAX + 1}"


# ── atomic writes / corruption recovery ─────────────────────────────────

def test_write_leaves_no_temp_file_behind(_isolate_memory_path):
    mm.save_memory({"notes": {"a": {"value": "1", "updated": "2026-01-01"}}})
    assert list(_isolate_memory_path.parent.glob("long_term.tmp-*")) == []
    assert _isolate_memory_path.exists()


def test_corrupted_file_is_backed_up_not_silently_discarded(_isolate_memory_path):
    _isolate_memory_path.parent.mkdir(parents=True, exist_ok=True)
    _isolate_memory_path.write_text("{not valid json", encoding="utf-8")

    mem = mm.load_memory()

    assert mem == mm._empty_memory()
    backups = list(_isolate_memory_path.parent.glob("long_term.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"
    # the corrupt file no longer sits at the real path — next save starts fresh, not silently
    assert not _isolate_memory_path.exists()


def test_non_dict_json_is_treated_as_corrupt_and_backed_up(_isolate_memory_path):
    _isolate_memory_path.parent.mkdir(parents=True, exist_ok=True)
    _isolate_memory_path.write_text("[1, 2, 3]", encoding="utf-8")

    mem = mm.load_memory()

    assert mem == mm._empty_memory()
    assert list(_isolate_memory_path.parent.glob("long_term.corrupt-*.json"))


def test_a_save_after_corruption_does_not_lose_the_backup(_isolate_memory_path):
    _isolate_memory_path.parent.mkdir(parents=True, exist_ok=True)
    _isolate_memory_path.write_text("{broken", encoding="utf-8")

    mm.update_memory({"notes": {"x": {"value": "y"}}})  # triggers a load (backs up) then a fresh save

    backups = list(_isolate_memory_path.parent.glob("long_term.corrupt-*.json"))
    assert len(backups) == 1
    assert _isolate_memory_path.exists()
    assert mm.load_memory()["notes"]["x"]["value"] == "y"


# ── concurrency: no lost updates across the single-lock read-modify-write ──

def test_concurrent_update_memory_calls_do_not_lose_writes():
    threads = [
        threading.Thread(target=mm.update_memory, args=({"notes": {f"k{i}": {"value": str(i)}}},))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    mem = mm.load_memory()
    assert len(mem["notes"]) == 20
    for i in range(20):
        assert mem["notes"][f"k{i}"]["value"] == str(i)
