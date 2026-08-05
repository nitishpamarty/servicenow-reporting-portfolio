"""
Generates synthetic ITSM data (incidents, problems, change requests) spread
across the last 18 months and posts it to a ServiceNow PDI via the REST
Table API. See ../servicenow-portfolio-project-brief.md for the full plan.

Usage:
    python3 generate_data.py                       # full run, default volumes
    python3 generate_data.py --incidents 100 --problems 10 --changes 20  # smaller test run
    python3 generate_data.py --workers 6            # concurrency

Design notes (why, not what):
  - priority is derived from impact+urgency via ServiceNow's own matrix so a
    platform business rule can't silently overwrite it away from what we log.
  - a "hotspot" business app receives a disproportionate share of P2
    incidents on purpose -- this is the seeded finding the brief asks for.
  - a small percentage of records get intentional data-quality gaps (blank
    category/assignment_group, orphaned CI reference) since identifying data
    quality issues is explicitly part of the target job description.
"""
import argparse
import csv
import os
import random
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from reference_data import build_reference_data
from snow_client import SnowClient

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# ---------------------------------------------------------------------------
# Time range: 18 months ending today
# ---------------------------------------------------------------------------
NOW = datetime.now()
RANGE_DAYS = 548  # ~18 months
RANGE_START = NOW - timedelta(days=RANGE_DAYS)
RECENT_CUTOFF_DAYS = 21  # records opened within this window are treated as "still in flight"

# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------
# impact/urgency -> priority, per ServiceNow's stock matrix
PRIORITY_MATRIX = {
    (1, 1): "1", (1, 2): "2", (2, 1): "2",
    (1, 3): "3", (2, 2): "3", (3, 1): "3",
    (2, 3): "4", (3, 2): "4", (3, 3): "4",
}
IMPACT_URGENCY_WEIGHTS = [
    ((1, 1), 3), ((1, 2), 7), ((2, 1), 8),
    ((1, 3), 6), ((2, 2), 24), ((3, 1), 7),
    ((2, 3), 15), ((3, 2), 15), ((3, 3), 15),
]

CATEGORY_SUBCATS = {
    "software": ["internal application", "os", "email"],
    "hardware": ["cpu", "memory", "disk", "keyboard", "mouse", "monitor"],
    "network": ["vpn", "dns", "wireless", "ip address", "dhcp"],
    "database": ["oracle", "sql server", "db2"],
    "inquiry": [],
    "password_reset": [],
}
CATEGORY_WEIGHTS = [("software", 30), ("hardware", 18), ("network", 16),
                     ("database", 12), ("inquiry", 14), ("password_reset", 10)]
CATEGORY_TO_GROUPS = {
    "software": ["Application Development", "Help Desk"],
    "hardware": ["Help Desk", "ITSM Engineering"],
    "network": ["Network", "ITSM Engineering"],
    "database": ["Database", "ITSM Engineering"],
    "inquiry": ["Help Desk"],
    "password_reset": ["Help Desk"],
}

CONTACT_TYPE_WEIGHTS = [("self-service", 32), ("email", 22), ("phone", 20),
                         ("virtual_agent", 14), ("chat", 8), ("walk-in", 2), ("monitoring", 2)]

INCIDENT_CLOSE_CODES = ["Solution provided", "Workaround provided", "Resolved by caller",
                         "Resolved by change", "Resolved by problem", "Resolved by request",
                         "User error", "Known error", "No resolution provided", "Duplicate"]

# SLA target hours by priority -- used to compute made_sla and to keep
# resolution-time distributions distinguishable per priority in reports.
SLA_HOURS = {"1": 4, "2": 8, "3": 24, "4": 72, "5": 120}
SLA_BREACH_RATE = {"1": 0.30, "2": 0.22, "3": 0.15, "4": 0.08, "5": 0.05}

PROBLEM_CATEGORY_WEIGHTS = [("software", 35), ("hardware", 20), ("network", 20), ("database", 25)]
PROBLEM_STATE_WEIGHTS_OLD = [("107", 78), ("106", 12), ("104", 6), ("103", 3), ("102", 1)]
PROBLEM_STATE_WEIGHTS_RECENT = [("101", 25), ("102", 25), ("103", 25), ("104", 15), ("106", 8), ("107", 2)]
# "duplicate" deliberately excluded -- it requires a Duplicate-of reference to another
# problem, which adds ordering complexity this generator doesn't need for realism.
RESOLUTION_CODES = ["fix_applied", "risk_accepted", "canceled"]

CHANGE_TYPE_WEIGHTS = [("standard", 45), ("normal", 45), ("emergency", 8), ("model", 2)]
CHANGE_RISK_WEIGHTS = [("4", 40), ("3", 35), ("2", 18), ("1", 5), ("5", 2)]
CHANGE_CATEGORY_WEIGHTS = [("Software", 25), ("Hardware", 15), ("Network", 15),
                            ("Applications Software", 15), ("System Software", 10),
                            ("Service", 10), ("Other", 10)]
CHANGE_STATE_WEIGHTS_OLD = [("3", 88), ("4", 8), ("0", 4)]
CHANGE_STATE_WEIGHTS_RECENT = [("-5", 15), ("-4", 15), ("-3", 15), ("-2", 20), ("-1", 15), ("0", 10), ("3", 10)]
CHANGE_CLOSE_CODE_WEIGHTS = [("successful", 78), ("successful_issues", 15), ("unsuccessful", 7)]


def weighted_choice(weights):
    items, w = zip(*weights)
    return random.choices(items, weights=w, k=1)[0]


def random_business_leaning_datetime(start, end):
    """Uniform day across the range, but skew hour-of-day toward business hours."""
    span_seconds = int((end - start).total_seconds())
    day_offset = random.randint(0, span_seconds)
    dt = start + timedelta(seconds=day_offset)
    if random.random() < 0.75:
        hour = random.choices(range(24), weights=[1]*7 + [6]*11 + [2]*6)[0]
    else:
        hour = random.randint(0, 23)
    return dt.replace(hour=hour, minute=random.randint(0, 59), second=random.randint(0, 59), microsecond=0)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def is_recent(dt):
    return (NOW - dt).days < RECENT_CUTOFF_DAYS


def pick_priority():
    iu = weighted_choice(IMPACT_URGENCY_WEIGHTS)
    return iu[0], iu[1], PRIORITY_MATRIX[iu]


def resolution_hours_for_priority(priority):
    base = {"1": 3, "2": 6, "3": 16, "4": 40, "5": 60}[priority]
    return max(0.25, random.lognormvariate(0, 0.9)) * base


class Faker:
    """Tiny local word-bank generator -- no external dependency needed."""
    SUBJECTS = ["VPN connection", "Order Management Platform", "email server", "laptop", "printer",
                "SAP Financial Accounting", "network switch", "database cluster", "Customer Self-Service Portal",
                "payroll batch job", "wireless access point", "shared drive", "mobile app", "SSO login",
                "file server", "billing report", "ETL pipeline", "point of sale terminal", "monitor",
                "conferencing tool"]
    PROBLEMS = ["is intermittently unavailable", "is running slower than usual", "fails during peak hours",
                "returns an error on save", "drops connections randomly", "is not syncing data correctly",
                "times out for some users", "shows stale data", "crashed unexpectedly",
                "is rejecting valid logins", "is stuck in a retry loop"]

    @classmethod
    def short_description(cls):
        return f"{random.choice(cls.SUBJECTS)} {random.choice(cls.PROBLEMS)}"

    @classmethod
    def description(cls, short_desc):
        detail = random.choice([
            "Reported by multiple users across the same team.",
            "Started after the most recent maintenance window.",
            "Reproducible only under high load.",
            "First noticed this morning, persists intermittently.",
            "Affects a subset of users in one region.",
            "No clear pattern identified yet; still gathering details.",
        ])
        return f"{short_desc}. {detail}"


def pick_assignee(ref, group):
    """
    incident/change_request have an active business rule that aborts the
    write unless assigned_to is a member of assignment_group -- always pick
    from that group's member pool (topped up in reference_data.py).
    """
    members = ref["group_members"].get(group["sys_id"])
    if members:
        return random.choice(members)
    return random.choice(ref["users"])["sys_id"]


def maybe_apply_data_quality_gap(record, gap_log, record_type, identifier):
    """Injects the on-purpose data-quality issues the brief asks for."""
    roll = random.random()
    if roll < 0.02 and "category" in record:
        record["category"] = ""
        gap_log.append((record_type, identifier, "blank_category"))
    elif roll < 0.035 and "assignment_group" in record:
        record["assignment_group"] = ""
        gap_log.append((record_type, identifier, "blank_assignment_group"))
    elif roll < 0.045 and "cmdb_ci" in record:
        record["cmdb_ci"] = uuid.uuid4().hex  # syntactically valid sys_id, points to nothing
        gap_log.append((record_type, identifier, "orphaned_cmdb_ci"))
    return record


def build_incident(ref, hotspot_app_id, ci_pool, gap_log):
    opened_at = random_business_leaning_datetime(RANGE_START, NOW)
    impact, urgency, priority = pick_priority()
    category = weighted_choice(CATEGORY_WEIGHTS)
    subcats = CATEGORY_SUBCATS[category]
    subcategory = random.choice(subcats) if subcats else ""

    # Seeded finding: skew P2 incidents heavily toward the hotspot app.
    if priority == "2" and random.random() < 0.55:
        cmdb_ci = hotspot_app_id
    else:
        cmdb_ci = random.choice(ci_pool)

    group_pool_names = CATEGORY_TO_GROUPS[category]
    group_pool = [g for g in ref["groups"] if g["name"] in group_pool_names] or ref["groups"]
    assignment_group = random.choice(group_pool)
    caller = random.choice(ref["users"])
    assigned_to_id = pick_assignee(ref, assignment_group)

    short_desc = Faker.short_description()
    record = {
        "short_description": short_desc,
        "description": Faker.description(short_desc),
        "opened_at": fmt(opened_at),
        "priority": priority,
        "impact": str(impact),
        "urgency": str(urgency),
        "category": category,
        "subcategory": subcategory,
        "contact_type": weighted_choice(CONTACT_TYPE_WEIGHTS),
        "caller_id": caller["sys_id"],
        "opened_by": caller["sys_id"],
        "assigned_to": assigned_to_id,
        "assignment_group": assignment_group["sys_id"],
        "cmdb_ci": cmdb_ci,
    }

    if is_recent(opened_at):
        state = weighted_choice([("1", 30), ("2", 30), ("3", 10), ("6", 20), ("7", 10)])
    else:
        state = weighted_choice([("7", 80), ("6", 8), ("8", 4), ("1", 3), ("2", 3), ("3", 2)])
    record["state"] = state
    record["incident_state"] = state

    resolved_at = closed_at = None
    if state in ("6", "7", "8"):
        res_hours = resolution_hours_for_priority(priority)
        resolved_at = opened_at + timedelta(hours=res_hours)
        if resolved_at > NOW:
            resolved_at = NOW - timedelta(minutes=random.randint(5, 120))
        record["resolved_at"] = fmt(resolved_at)
        record["resolved_by"] = assigned_to_id
        if state != "8":
            record["close_code"] = random.choice(INCIDENT_CLOSE_CODES)
            record["close_notes"] = "Issue addressed; verified with the reporting user." \
                if state == "7" else "Resolution applied, pending user confirmation."
        if state == "7":
            closed_at = resolved_at + timedelta(hours=random.uniform(0, 48))
            if closed_at > NOW:
                closed_at = NOW
            record["closed_at"] = fmt(closed_at)
            record["closed_by"] = assigned_to_id

        breach_rate = SLA_BREACH_RATE[priority]
        sla_target = SLA_HOURS[priority]
        breached = random.random() < breach_rate
        record["made_sla"] = "false" if breached else "true"
        if breached and res_hours < sla_target:
            # keep the flag internally consistent with the timestamps for anyone cross-checking
            record["made_sla"] = "true"

        if random.random() < 0.06:
            record["reopen_count"] = str(random.choice([1, 1, 1, 2]))
            record["reopened_by"] = random.choice(ref["users"])["sys_id"]
            reopen_time = resolved_at + timedelta(hours=random.uniform(1, 36))
            if reopen_time < NOW:
                record["reopened_time"] = fmt(reopen_time)

    return maybe_apply_data_quality_gap(record, gap_log, "incident", short_desc)


# The "Problem Model: Check State Transition" business rule rejects a
# create/update that skips states, even via the REST API on insert -- so a
# problem has to be walked through each state with a separate PATCH rather
# than created directly in its final state.
PROBLEM_STATE_ORDER = ["101", "102", "103", "104", "106", "107"]


def build_problem(ref, hotspot_app_id, ci_pool):
    opened_at = random_business_leaning_datetime(RANGE_START, NOW)
    category = weighted_choice(PROBLEM_CATEGORY_WEIGHTS)
    short_desc = f"Recurring: {Faker.short_description()}"

    cmdb_ci = hotspot_app_id if random.random() < 0.2 else random.choice(ci_pool)
    group_pool_names = CATEGORY_TO_GROUPS[category]
    group_pool = [g for g in ref["groups"] if g["name"] in group_pool_names] or ref["groups"]
    assignment_group = random.choice(group_pool)
    assigned_to_id = pick_assignee(ref, assignment_group)

    base_record = {
        "short_description": short_desc,
        "description": Faker.description(short_desc),
        "opened_at": fmt(opened_at),
        "opened_by": random.choice(ref["users"])["sys_id"],
        "category": category,
        "assignment_group": assignment_group["sys_id"],
        "assigned_to": assigned_to_id,
        "cmdb_ci": cmdb_ci,
        "major_problem": "true" if random.random() < 0.08 else "false",
    }

    final_state = weighted_choice(PROBLEM_STATE_WEIGHTS_RECENT if is_recent(opened_at) else PROBLEM_STATE_WEIGHTS_OLD)
    target_idx = PROBLEM_STATE_ORDER.index(final_state)

    resolved_at = opened_at + timedelta(days=random.uniform(1, 30))
    if resolved_at > NOW:
        resolved_at = NOW - timedelta(hours=random.randint(1, 48))
    closed_at = resolved_at + timedelta(hours=random.uniform(0, 72))
    if closed_at > NOW:
        closed_at = NOW

    state_updates = []
    for state in PROBLEM_STATE_ORDER[1:target_idx + 1]:
        update = {"state": state, "problem_state": state}
        if state == "104":
            update["cause_notes"] = "Root cause identified through log analysis and CI change history review."
            update["fix_notes"] = "Permanent fix under active development."
        if state == "106":
            resolution_code = weighted_choice([(c, 1) for c in RESOLUTION_CODES])
            update["resolution_code"] = resolution_code
            update["resolved_at"] = fmt(resolved_at)
            update["resolved_by"] = assigned_to_id
            update["known_error"] = "true" if random.random() < 0.4 else "false"
            update["workaround_applied"] = "true" if random.random() < 0.5 else "false"
            update["fix_notes"] = "Permanent fix applied and validated in the affected environment." \
                if resolution_code == "fix_applied" else "No permanent fix applied; see resolution notes."
            update["close_notes"] = {
                "fix_applied": "Root cause resolved and fix validated.",
                "risk_accepted": "Underlying risk accepted by the business; no further action planned.",
                "canceled": "Investigation canceled; no longer impacting the business.",
            }[resolution_code]
        if state == "107":
            update["closed_at"] = fmt(closed_at)
            update["closed_by"] = assigned_to_id
        state_updates.append(update)

    return base_record, state_updates, opened_at


def build_change(ref, hotspot_app_id, ci_pool):
    opened_at = random_business_leaning_datetime(RANGE_START, NOW)
    change_type = weighted_choice(CHANGE_TYPE_WEIGHTS)
    risk = weighted_choice(CHANGE_RISK_WEIGHTS)
    category = weighted_choice(CHANGE_CATEGORY_WEIGHTS)
    priority = {"1": "1", "2": "2", "3": "2", "4": "3", "5": "4"}[risk]

    cmdb_ci = hotspot_app_id if random.random() < 0.12 else random.choice(ci_pool)
    assignment_group = random.choice(ref["groups"])
    assigned_to_id = pick_assignee(ref, assignment_group)
    requested_by = random.choice(ref["users"])

    short_desc = f"Change: {Faker.short_description().split(' is')[0].split(' fails')[0].split(' returns')[0]} update"
    record = {
        "short_description": short_desc,
        "description": f"{change_type.capitalize()} change to {category.lower()} components.",
        "opened_at": fmt(opened_at),
        "requested_by": requested_by["sys_id"],
        "opened_by": requested_by["sys_id"],
        "type": change_type,
        "risk": risk,
        "priority": priority,
        "category": category,
        "assignment_group": assignment_group["sys_id"],
        "assigned_to": assigned_to_id,
        "cmdb_ci": cmdb_ci,
        "cab_required": "false" if change_type == "standard" else "true",
    }

    start_date = opened_at + timedelta(days=random.uniform(1, 14))
    duration_hours = random.uniform(1, 6) if change_type != "emergency" else random.uniform(0.5, 3)
    end_date = start_date + timedelta(hours=duration_hours)
    record["start_date"] = fmt(start_date)
    record["end_date"] = fmt(end_date)

    if is_recent(opened_at):
        state = weighted_choice(CHANGE_STATE_WEIGHTS_RECENT)
    else:
        state = weighted_choice(CHANGE_STATE_WEIGHTS_OLD)
    record["state"] = state

    if state in ("3", "4"):
        record["closed_at"] = fmt(min(end_date + timedelta(hours=random.uniform(0, 24)), NOW))
        record["closed_by"] = assigned_to_id
        if state == "3":
            record["close_code"] = weighted_choice(CHANGE_CLOSE_CODE_WEIGHTS)
            record["close_notes"] = "Change implemented per plan." if record["close_code"] == "successful" \
                else "Change implemented with follow-up issues logged separately."

    return record


def write_csv(path, rows, fieldnames):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_batch(client, table, records, workers, label):
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.post, table, rec): rec for rec in records}
        done = 0
        for fut in as_completed(futures):
            rec = futures[fut]
            done += 1
            try:
                result = fut.result()
                results.append({"sys_id": result["sys_id"], "number": result.get("number", ""),
                                 "short_description": rec.get("short_description", "")})
            except Exception as exc:
                errors.append((rec.get("short_description", "?"), str(exc)))
            if done % 100 == 0 or done == len(records):
                print(f"  [{label}] {done}/{len(records)} posted ({len(errors)} errors)")
    return results, errors


def create_problem_with_transitions(client, base_record, state_updates):
    result = client.post("problem", base_record)
    sys_id = result["sys_id"]
    for update in state_updates:
        client.patch("problem", sys_id, update)
    return result


def run_problem_batch(client, problems, workers, label):
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(create_problem_with_transitions, client, base, updates): base
            for base, updates, _opened_at in problems
        }
        done = 0
        for fut in as_completed(futures):
            base = futures[fut]
            done += 1
            try:
                result = fut.result()
                results.append({"sys_id": result["sys_id"], "number": result.get("number", ""),
                                 "short_description": base.get("short_description", "")})
            except Exception as exc:
                errors.append((base.get("short_description", "?"), str(exc)))
            if done % 50 == 0 or done == len(problems):
                print(f"  [{label}] {done}/{len(problems)} posted ({len(errors)} errors)")
    return results, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--incidents", type=int, default=2500)
    parser.add_argument("--problems", type=int, default=180)
    parser.add_argument("--changes", type=int, default=450)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print("Loading reference data (users, groups, CIs, business apps)...")
    ref = build_reference_data()
    hotspot_app_id = ref["hotspot_app"]["sys_id"]
    ci_pool = [c["sys_id"] for c in ref["servers"] + ref["services"] + ref["business_apps"]]
    print(f"  {len(ref['users'])} users, {len(ref['groups'])} groups, "
          f"{len(ci_pool)} CIs (hotspot={ref['hotspot_app']['name']})")

    client = SnowClient()
    gap_log = []

    print(f"\nGenerating {args.problems} problems...")
    problems = [build_problem(ref, hotspot_app_id, ci_pool) for _ in range(args.problems)]
    print(f"Posting problems (create + state-transition walk) with {args.workers} workers...")
    problem_results, problem_errors = run_problem_batch(client, problems, args.workers, "problems")

    print(f"\nGenerating {args.changes} changes...")
    change_records = [build_change(ref, hotspot_app_id, ci_pool) for _ in range(args.changes)]
    print(f"Posting changes with {args.workers} workers...")
    change_results, change_errors = run_batch(client, "change_request", change_records, args.workers, "changes")

    print(f"\nGenerating {args.incidents} incidents...")
    incident_records = [build_incident(ref, hotspot_app_id, ci_pool, gap_log) for _ in range(args.incidents)]
    print(f"Posting incidents with {args.workers} workers...")
    incident_results, incident_errors = run_batch(client, "incident", incident_records, args.workers, "incidents")

    # Linking pass: a subset of incidents point at a problem or a change that caused them.
    print("\nLinking a subset of incidents to problems/changes...")
    linked = 0
    if problem_results and incident_results:
        link_targets = random.sample(incident_results, k=min(len(incident_results), int(0.12 * len(incident_results))))
        for inc in link_targets:
            problem = random.choice(problem_results)
            try:
                client.patch("incident", inc["sys_id"], {"problem_id": problem["sys_id"]})
                linked += 1
            except Exception as exc:
                print(f"  link error (problem): {exc}")
    if change_results and incident_results:
        link_targets = random.sample(incident_results, k=min(len(incident_results), int(0.08 * len(incident_results))))
        for inc in link_targets:
            change = random.choice(change_results)
            try:
                client.patch("incident", inc["sys_id"], {"caused_by": change["sys_id"]})
                linked += 1
            except Exception as exc:
                print(f"  link error (change): {exc}")
    print(f"  {linked} link updates applied")

    # Reference logs for debugging/spot-checking (gitignored, not committed)
    if incident_results:
        write_csv(os.path.join(DATA_DIR, "incidents_created.csv"), incident_results,
                  ["sys_id", "number", "short_description"])
    if problem_results:
        write_csv(os.path.join(DATA_DIR, "problems_created.csv"), problem_results,
                  ["sys_id", "number", "short_description"])
    if change_results:
        write_csv(os.path.join(DATA_DIR, "changes_created.csv"), change_results,
                  ["sys_id", "number", "short_description"])
    if gap_log:
        with open(os.path.join(DATA_DIR, "data_quality_gaps.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["record_type", "short_description", "gap_type"])
            writer.writerows(gap_log)

    print("\n=== Summary ===")
    print(f"Incidents: {len(incident_results)} created, {len(incident_errors)} errors")
    print(f"Problems:  {len(problem_results)} created, {len(problem_errors)} errors")
    print(f"Changes:   {len(change_results)} created, {len(change_errors)} errors")
    print(f"Data-quality gaps injected: {len(gap_log)}")
    print(f"Incident/problem/change links applied: {linked}")
    print(f"Hotspot app for P2 concentration: {ref['hotspot_app']['name']} ({hotspot_app_id})")

    for label, errs in [("incident", incident_errors), ("problem", problem_errors), ("change", change_errors)]:
        for desc, err in errs[:5]:
            print(f"  [{label} error] {desc}: {err}")


if __name__ == "__main__":
    main()
