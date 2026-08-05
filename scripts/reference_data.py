"""
Fetches/creates the reference entities (users, groups, CIs) that the
generated ITSM records point to. Idempotent: safe to re-run, existing
records are matched by name instead of duplicated.
"""
import json
import os
import random

from snow_client import SnowClient

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "reference_cache.json")

ITSM_GROUP_NAMES = [
    "Help Desk",
    "Network",
    "Database",
    "Incident Management",
    "Problem Analyzers",
    "Application Development",
    "ITSM Engineering",
    "IT Securities",
]

# The business apps we seed ourselves (cmdb_ci_business_app is empty on a
# fresh PDI). One of these ("Order Management Platform") is the deliberate
# P2-incident hotspot referenced in the project brief.
BUSINESS_APPS = [
    {
        "name": "Order Management Platform",
        "short_description": "Customer order intake, pricing, and fulfillment orchestration",
        "business_criticality": "high",
    },
    {
        "name": "Customer Self-Service Portal",
        "short_description": "External-facing customer account and support portal",
        "business_criticality": "medium",
    },
    {
        "name": "Employee Payroll Processing",
        "short_description": "Bi-weekly payroll calculation and disbursement system",
        "business_criticality": "high",
    },
    {
        "name": "HR Employee Self-Service",
        "short_description": "Employee benefits, timesheets, and HR case management",
        "business_criticality": "low",
    },
    {
        "name": "Data Warehouse ETL Service",
        "short_description": "Nightly ETL pipelines feeding the enterprise data warehouse",
        "business_criticality": "medium",
    },
    {
        "name": "Field Service Mobile App",
        "short_description": "Mobile app for field technician dispatch and work orders",
        "business_criticality": "medium",
    },
    {
        "name": "Vendor Invoicing System",
        "short_description": "Accounts payable invoice matching and vendor payment system",
        "business_criticality": "low",
    },
    {
        "name": "Internal Analytics Dashboarding",
        "short_description": "Internal BI dashboards used by finance and ops leadership",
        "business_criticality": "low",
    },
]

# Existing PDI demo services we like to hang relationships off of.
PREFERRED_SERVICE_NAMES = [
    "IT Services",
    "Email",
    "E-Commerce",
    "Retail POS (Point of Sale)",
    "SAP Financial Accounting",
    "PeopleSoft HRMS",
]

REL_TYPE_DEPENDS_ON = "Depends on::Used by"
REL_TYPE_RUNS_ON = "Runs on::Runs"


def _get_or_create_business_apps(client, users):
    existing = {a["name"]: a for a in client.get("cmdb_ci_business_app", fields="sys_id,name", limit=200)}
    apps = []
    for spec in BUSINESS_APPS:
        if spec["name"] in existing:
            apps.append({"sys_id": existing[spec["name"]]["sys_id"], "name": spec["name"]})
            continue
        owner = random.choice(users)
        body = {
            "name": spec["name"],
            "short_description": spec["short_description"],
            "business_criticality": spec["business_criticality"],
            "it_application_owner": owner["sys_id"],
            "operational_status": "1",  # Operational
            "install_status": "1",  # Installed
        }
        result = client.post("cmdb_ci_business_app", body)
        apps.append({"sys_id": result["sys_id"], "name": spec["name"]})
    return apps


def _get_rel_type_sys_id(client, name):
    result = client.get("cmdb_rel_type", query=f"name={name}", fields="sys_id,name", limit=1)
    if not result:
        raise RuntimeError(f"cmdb_rel_type '{name}' not found")
    return result[0]["sys_id"]


def _ensure_relationship(client, existing_pairs, parent, child, rel_type_sys_id):
    key = (parent, child, rel_type_sys_id)
    if key in existing_pairs:
        return
    client.post("cmdb_rel_ci", {"parent": parent, "child": child, "type": rel_type_sys_id})
    existing_pairs.add(key)


def _ensure_group_membership(client, groups, users, min_members=6):
    """
    Incident/change_request have an active business rule that aborts the
    write unless `assigned_to` is already a member of `assignment_group`.
    Top up every ITSM group to a minimum member count so there's always a
    valid pool of assignees per group.
    """
    group_ids = [g["sys_id"] for g in groups]
    query = "^OR".join(f"group={g}" for g in group_ids)
    existing = client.get("sys_user_grmember", query=query, fields="group,user", limit=2000)

    members_by_group = {g["sys_id"]: set() for g in groups}
    for m in existing:
        gid = m["group"]["value"] if isinstance(m["group"], dict) else m["group"]
        uid = m["user"]["value"] if isinstance(m["user"], dict) else m["user"]
        members_by_group.setdefault(gid, set()).add(uid)

    user_ids = [u["sys_id"] for u in users]
    for group in groups:
        gid = group["sys_id"]
        current = members_by_group[gid]
        needed = min_members - len(current)
        if needed <= 0:
            continue
        candidates = [u for u in user_ids if u not in current]
        random.shuffle(candidates)
        for uid in candidates[:needed]:
            client.post("sys_user_grmember", {"group": gid, "user": uid})
            current.add(uid)

    return {gid: sorted(members) for gid, members in members_by_group.items()}


def build_reference_data(force_refresh=False):
    if os.path.exists(CACHE_PATH) and not force_refresh:
        with open(CACHE_PATH) as f:
            return json.load(f)

    client = SnowClient()

    print("Fetching active users...")
    users = client.get(
        "sys_user",
        query="active=true^user_nameISNOTEMPTY",
        fields="sys_id,user_name,name",
        limit=200,
    )
    if len(users) < 10:
        raise RuntimeError("Too few active users on instance to generate realistic data")

    print("Fetching ITSM assignment groups...")
    group_query = "^OR".join(f"name={n}" for n in ITSM_GROUP_NAMES)
    groups = client.get("sys_user_group", query=group_query, fields="sys_id,name", limit=50)
    found_names = {g["name"] for g in groups}
    missing = set(ITSM_GROUP_NAMES) - found_names
    if missing:
        print(f"  warning: groups not found on instance, skipping: {missing}")

    print("Fetching existing servers...")
    servers = client.get("cmdb_ci_server", fields="sys_id,name", limit=100)

    print("Fetching existing services...")
    all_services = client.get("cmdb_ci_service", fields="sys_id,name", limit=200)
    services = [s for s in all_services if s["name"] in PREFERRED_SERVICE_NAMES] or all_services[:6]

    print("Creating/verifying synthetic business apps...")
    business_apps = _get_or_create_business_apps(client, users)

    print("Linking business apps to servers/services in CMDB (cmdb_rel_ci)...")
    existing_rel_pairs = set()
    depends_on_type = _get_rel_type_sys_id(client, REL_TYPE_DEPENDS_ON)
    runs_on_type = _get_rel_type_sys_id(client, REL_TYPE_RUNS_ON)
    for app in business_apps:
        service = random.choice(services)
        server = random.choice(servers)
        _ensure_relationship(client, existing_rel_pairs, app["sys_id"], service["sys_id"], depends_on_type)
        _ensure_relationship(client, existing_rel_pairs, app["sys_id"], server["sys_id"], runs_on_type)

    print("Ensuring every ITSM group has enough members (required by the "
          "'assigned_to must be a member of assignment_group' business rule)...")
    group_members = _ensure_group_membership(client, groups, users)

    data = {
        "users": users,
        "groups": groups,
        "group_members": group_members,
        "servers": servers,
        "services": services,
        "business_apps": business_apps,
        "hotspot_app": next(a for a in business_apps if a["name"] == "Order Management Platform"),
    }

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)

    return data


if __name__ == "__main__":
    ref = build_reference_data(force_refresh=True)
    print(f"\nUsers: {len(ref['users'])}")
    print(f"Groups: {len(ref['groups'])} -> {[g['name'] for g in ref['groups']]}")
    print(f"Servers: {len(ref['servers'])}")
    print(f"Services: {len(ref['services'])} -> {[s['name'] for s in ref['services']]}")
    print(f"Business apps: {len(ref['business_apps'])} -> {[a['name'] for a in ref['business_apps']]}")
    print(f"Hotspot app: {ref['hotspot_app']}")
