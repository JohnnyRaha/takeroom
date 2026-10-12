"""Check the preset library loads, serves, and that user overrides merge."""
import json, os, shutil, sys, tempfile
DATA = tempfile.mkdtemp(prefix="presets-")
os.environ["TAKEROOM_BACKEND"] = "mock"; os.environ["TAKEROOM_DATA"] = DATA
from fastapi.testclient import TestClient
from takeroom.main import app
from takeroom.config import settings

FAILED = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not ok: FAILED.append(label)

with TestClient(app) as c:
    print("\nlibrary")
    lib = c.get("/api/prompts").json()
    check("serves", lib["count"] > 0, str(lib["count"]))
    check("no load errors", not lib["errors"], str(lib["errors"]))

    print("\nevery preset is well formed")
    ids, bad = set(), []
    for cat in lib["categories"]:
        for p in cat["presets"]:
            if p["id"] in ids: bad.append(f"duplicate id {p['id']}")
            ids.add(p["id"])
            for key in ("id", "name", "prompt", "duration"):
                if not p.get(key): bad.append(f"{p.get('id')} missing {key}")
            if len(p["prompt"].split(",")) < 5:
                bad.append(f"{p['id']} prompt has too few tags")
            if not p.get("bpm"): bad.append(f"{p['id']} has no bpm")
    check("no duplicate ids / missing fields", not bad, "; ".join(bad[:4]))
    check("all ids unique", len(ids) == lib["count"], f"{len(ids)} vs {lib['count']}")

    print("\nlookup")
    one = c.get("/api/prompts/bb-dusty-break")
    check("by id", one.status_code == 200 and one.json()["bpm"] == 92)
    check("carries category", one.json().get("category") == "drums-boom-bap")
    check("unknown is 404", c.get("/api/prompts/nope").status_code == 404)

    print("\nuser overrides")
    user = {"categories": [
        {"id": "drums-boom-bap", "name": "Boom bap drums", "presets": [
            {"id": "bb-dusty-break", "name": "MY VERSION", "prompt": "a,b,c,d,e,f", "bpm": 95, "duration": 30},
            {"id": "bb-mine", "name": "New one", "prompt": "a,b,c,d,e,f", "bpm": 88, "duration": 30}]},
        {"id": "my-cat", "name": "My category", "presets": [
            {"id": "mine-1", "name": "Mine", "prompt": "a,b,c,d,e,f", "bpm": 90, "duration": 30}]}]}
    (settings.data_dir / "prompts.user.json").write_text(json.dumps(user))
    lib2 = c.get("/api/prompts").json()
    over = c.get("/api/prompts/bb-dusty-break").json()
    check("user preset overrides built-in", over["name"] == "MY VERSION", over["name"])
    check("new preset added to existing category", c.get("/api/prompts/bb-mine").status_code == 200)
    check("new category added", any(x["id"] == "my-cat" for x in lib2["categories"]))
    check("count grew by two", lib2["count"] == lib["count"] + 2, f"{lib['count']} -> {lib2['count']}")

    print("\nbad user file doesn't break the library")
    (settings.data_dir / "prompts.user.json").write_text("{not json")
    lib3 = c.get("/api/prompts").json()
    check("built-ins still served", lib3["count"] == lib["count"], str(lib3["count"]))
    check("error reported", bool(lib3["errors"]), str(lib3["errors"])[:120])

    print("\nui")
    r = c.get("/static/prompts.js")
    check("picker served", r.status_code == 200 and "applyPreset" in r.text)

shutil.rmtree(DATA, ignore_errors=True)
print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all preset checks passed'}\n")
sys.exit(1 if FAILED else 0)
