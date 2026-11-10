"""Foxglove guard: Claude Code hook (PreToolUse + PostToolUse).

Asks Yanling before:
  1. SQL writes sent to a database (INSERT/UPDATE/DELETE/EXEC/DDL ...),
  2. anything touching patient information: patient-data folders, health card
     numbers, dates of birth, Kroll queries on tables not listed as non-patient,
  3. changes to the guard or Claude settings.

Session approvals: when Yanling clicks Yes on a guard prompt and the action runs,
the PostToolUse half records it for that session (12 h max), so the same kind of
action isn't asked again. Patients are approved one at a time (per patient
folder or per health card number). Destructive SQL (DROP, TRUNCATE, ALTER, ...)
and guard changes always ask. Approvals are only recorded after a prompt was
answered Yes and the tool ran: typed text or messages from other sessions can't
create one. Approval files hold hashes only, no patient details.

Never auto-approves: nothing flagged = silent, normal permissions apply.
Fails closed: an internal error asks.
Settings: foxglove-guard.json next to this file.
"""
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
MAX_AGE = 12 * 3600

DEFAULT_CONFIG = {
    "phiPaths": ["phi/", "patient-data/", "real-faxes/", "beta-consented/"],
    "syntheticPaths": ["tests/fixtures/synthetic/"],
    "nonPhiTables": [],
    "ignoreNumbers": [],
    "sqlContextPattern": r"sqlcmd|osql|invoke-sqlcmd|\bbcp\b|pyodbc|pymssql|sqlalchemy|mssql|tedious|sqlconnection|krollserver|server\s*=|\.sql\b",
}

SQL_WRITE_RE = re.compile(
    r"\b(insert\s+into|update\s+[\w.\[\]\"]+\s+set|delete\s+from|delete\s+[\w.\[\]\"]+\s+where|"
    r"merge\s+(into\s+)?[\w.\[\]\"]+|drop\s+(table|view|proc|procedure|database|index|function|trigger)|"
    r"alter\s+(table|view|proc|procedure|database|function|trigger)|truncate\s+table|"
    r"create\s+(table|view|proc|procedure|database|index|function|trigger)|exec(ute)?\s+[\w.\[\]]+|"
    r"sp_\w+|xp_\w+|grant\s+|revoke\s+|deny\s+|bulk\s+insert|bcp\s+\S+\s+in\b)",
    re.I,
)
DESTRUCTIVE_RE = re.compile(r"^(DROP|TRUNCATE|ALTER|GRANT|REVOKE|DENY)\b|^CREATE DATABASE")
DOB_RE = re.compile(r"\b(dob|d\.o\.b|date of birth|birth\s*date)\b[\s:=-]*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", re.I)
HCN_RE = re.compile(r"\b(\d{4})[- ]?(\d{3})[- ]?(\d{3})(?:[- ]?[A-Z]{2})?\b")
TABLE_RE = re.compile(r"\b(?:from|join)\s+([\w.\[\]\"]+)", re.I)
SCRIPT_RE = re.compile(
    r"""(?:^|\s)(?:-i\s+|-File\s+|-InputFile\s+)?["']?([^\s"'|;&<>]+\.(?:py|js|mjs|cjs|ts|ps1|sql|bat|cmd|sh))["']?""",
    re.I,
)
# Only these .claude files are the guard; skills/agents/launch.json are not.
GUARD_FILE_RE = re.compile(r"(^|[/\\])\.claude[/\\](settings(\.local)?\.json|hooks([/\\]|$))", re.I)
READONLY_CMD_RE = re.compile(r"^\s*(cat|type|ls|dir|head|tail|git\s+(status|diff|log|show))\b", re.I)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(os.path.join(HERE, "foxglove-guard.json"), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def norm(p):
    return str(p or "").replace("\\", "/").lower()


def under_any(path, prefixes):
    f = norm(path)
    return any(f.startswith(norm(p)) or ("/" + norm(p)) in f for p in prefixes)


def luhn_ok(digits):
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def health_card_numbers(text, ignore=()):
    out = []
    for m in HCN_RE.finditer(text):
        digits = m.group(1) + m.group(2) + m.group(3)
        if luhn_ok(digits) and len(set(digits)) > 1 and digits not in ignore:
            out.append(digits)
    return out


def read_tables(text):
    seen = []
    for m in TABLE_RE.finditer(text):
        t = re.sub(r"[\[\]\"]", "", m.group(1)).split(".")[-1].lower()
        if t and t not in ("select", "where", "on") and t not in seen:
            seen.append(t)
    return seen


def scripts_in_command(cmd, cwd):
    out = []
    for m in SCRIPT_RE.finditer(cmd):
        rel = m.group(1)
        p = rel if os.path.isabs(rel) else os.path.join(cwd or ".", rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) < 2_000_000:
                with open(p, encoding="utf-8", errors="replace") as f:
                    out.append((rel, f.read()))
        except Exception:
            pass
    return out


def check(inp, cfg):
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    cwd = inp.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."
    found = []  # (key, reason, always_ask)

    def add(key, reason, always=False):
        found.append((key, reason, always))

    file_path = ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or ""
    paths = [file_path] if file_path else []
    texts = []
    is_shell = tool in ("Bash", "PowerShell")
    cmd = str(ti.get("command") or "") if is_shell else ""

    if is_shell:
        texts.append(("command", cmd))
        for rel, body in scripts_in_command(cmd, cwd):
            texts.append((rel, body))
            paths.append(rel)
        paths += [t for t in re.split(r"[\s\"'|;&<>]+", cmd) if "/" in t or "\\" in t]
    elif tool == "Write":
        texts.append((file_path, str(ti.get("content") or "")))
    elif tool in ("Edit", "MultiEdit"):
        edits = ti.get("edits") or [{"new_string": ti.get("new_string")}]
        texts.append((file_path, "\n".join(str(e.get("new_string") or "") for e in edits)))
    elif tool in ("Read", "Glob", "Grep"):
        if tool == "Grep" and ti.get("pattern"):
            texts.append(("grep pattern", str(ti["pattern"])))
    else:  # MCP and any other tool: inspect the whole input
        texts.append((tool, json.dumps(ti)))

    # 3. Guard self-protection
    if tool not in ("Read", "Glob", "Grep"):
        if any(GUARD_FILE_RE.search(p) for p in paths) or (
            is_shell and GUARD_FILE_RE.search(cmd) and not READONLY_CMD_RE.match(cmd)
        ):
            add("guard", "changes the Foxglove guard / Claude settings", True)

    # 2a. Patient-data folders: one approval per patient (first subfolder; a loose file = its own)
    for p in paths:
        f = norm(p)
        for pre in map(norm, cfg["phiPaths"]):
            if f.startswith(pre):
                rest = f[len(pre):]
            elif ("/" + pre) in f:
                rest = f[f.index("/" + pre) + 1 + len(pre):]
            else:
                continue
            seg = rest.split("/")[0]
            if seg:  # the folder itself (listing it) names no patient yet
                add("patient:" + pre + seg, "uses patient data in " + pre + seg)

    all_synthetic = bool(paths) and all(under_any(p, cfg["syntheticPaths"]) for p in paths)
    sql_ctx = re.compile(cfg["sqlContextPattern"], re.I)
    cmd_is_sql = is_shell and bool(sql_ctx.search(cmd))
    non_phi = [t.lower() for t in cfg["nonPhiTables"]]
    ignore = set(re.sub(r"\D", "", str(n)) for n in cfg.get("ignoreNumbers", []))

    for where, text in texts:
        if not text:
            continue
        runs = tool not in ("Write", "Edit", "MultiEdit")  # authoring code is not running it
        in_sql = cmd_is_sql or where.lower().endswith(".sql") or bool(sql_ctx.search(text))

        # 1. SQL writes
        m = SQL_WRITE_RE.search(text) if runs and in_sql else None
        if m:
            kw = " ".join(m.group(0).split()[:2]).upper()
            destructive = bool(DESTRUCTIVE_RE.search(kw))
            add("sql-destructive" if destructive else "sql-write", "SQL write (%s) in %s" % (kw, where), destructive)

        # 2b. Kroll reads of patient tables
        if runs and re.search(r"krollserver", text, re.I):
            unknown = [t for t in read_tables(text) if t not in non_phi]
            if unknown:
                add("kroll-phi-read", "Kroll query reads patient table(s): " + ", ".join(unknown[:5]))

        # 2c. Patient identifiers in content (not in synthetic test data)
        if not all_synthetic and not under_any(where, cfg["syntheticPaths"]):
            for d in health_card_numbers(text, ignore):
                add("patient-hcn:" + d, "health card number ...%s in %s" % (d[6:], where))
            if DOB_RE.search(text):
                add("dob:" + norm(where), "date of birth in " + where)

    return found


# ---- session approval state (git-ignored; hashed keys only) ----
def h(s):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()[:32]


def sid(inp):
    return h(inp.get("session_id") or "no-session")


def call_id(inp):
    return h((inp.get("tool_name") or "") + json.dumps(inp.get("tool_input") or {}, sort_keys=True))


def grants_file(inp):
    return os.path.join(STATE_DIR, "grants-%s.json" % sid(inp))


def pending_file(inp):
    return os.path.join(STATE_DIR, "pending-%s-%s.json" % (sid(inp), call_id(inp)))


def read_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def live_grants(inp):
    g = read_json(grants_file(inp), {})
    now = time.time()
    return {k: v for k, v in g.items() if now - v <= MAX_AGE}


def cleanup():
    try:
        now = time.time()
        for name in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, name)
            if now - os.path.getmtime(p) > MAX_AGE:
                os.remove(p)
    except Exception:
        pass


def respond(decision, reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def pre_tool_use(inp):
    cleanup()
    grants = live_grants(inp)
    need = [f for f in check(inp, load_config()) if f[2] or h(f[0]) not in grants]
    if not need:
        sys.exit(0)  # nothing new flagged: normal permissions apply
    write_json(pending_file(inp), [h(k) for k, _, always in need if not always])
    once = " (Yes = approved for the rest of this session)" if any(not a for _, _, a in need) else ""
    reasons = []
    for _, r, _ in need:
        if r not in reasons:
            reasons.append(r)
    respond("ask", "Foxglove guard: needs Yanling's OK" + once + ": " + "; ".join(reasons))


def post_tool_use(inp):
    pf = pending_file(inp)
    keys = read_json(pf, None)
    if keys is None:
        return
    g = live_grants(inp)
    for k in keys:
        g[k] = time.time()
    write_json(grants_file(inp), g)
    try:
        os.remove(pf)
    except Exception:
        pass


def main():
    inp = {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        inp = json.loads(raw or "{}")
        if inp.get("hook_event_name") == "PostToolUse":
            post_tool_use(inp)
            sys.exit(0)
        pre_tool_use(inp)
    except SystemExit:
        raise
    except Exception as e:  # fail closed
        if inp.get("hook_event_name") == "PostToolUse":
            sys.exit(0)
        respond("ask", "Foxglove guard couldn't check this action (%s), so it's asking to be safe." % e)


if __name__ == "__main__":
    main()
