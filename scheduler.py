"""
University of Mkar Examination Timetable Scheduler — prototype
Python 3.10+. Uses only the Python standard library.

Input CSV files (headers shown in README.md):
  students.csv: student_id,matric_number,programme,level
  courses.csv: course_id,course_code,course_title,duration_minutes
  registrations.csv: student_id,course_id
  slots.csv: slot_id,date,start_time,end_time
  venues.csv: venue_id,venue_name,capacity
  invigilators.csv: invigilator_id,name,department,max_duties,qualified_courses
  availability.csv: invigilator_id,slot_id,available

qualified_courses is a semicolon-separated list of course IDs; use * for all courses.
The prototype assumes one venue per course and one slot per course. For large
instances or complex policies, replace the backtracking solver with OR-Tools CP-SAT.
"""
from __future__ import annotations
import csv
import json
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DATA = PROJECT_DIR / "data"
OUT = PROJECT_DIR / "output"

def read_csv(name):
    path = DATA / name
    if not path.exists():
        raise FileNotFoundError(f"Missing input file: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(line for line in f if line.strip()))


def write_csv(name, rows, fields):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def overlaps(slot_a, slot_b):
    # Slots use ISO-like HH:MM strings and dates in YYYY-MM-DD format.
    if slot_a["date"] != slot_b["date"]:
        return False
    return (slot_a["start_time"] < slot_b["end_time"] and
            slot_b["start_time"] < slot_a["end_time"])


def find_invigilator_plan(data, course_slots, required_per_exam):
    invigilators = data["invigilators"]
    slot_by_id = data["slot_by_id"]
    sessions = []
    for cid, tid in course_slots.items():
        eligible = []
        for inv in invigilators:
            iid = inv["invigilator_id"]
            if not data["availability"].get((iid, tid), True):
                continue
            qualified_raw = (inv.get("qualified_courses") or "*").strip()
            if qualified_raw != "*" and cid not in {
                item.strip() for item in qualified_raw.split(";") if item.strip()
            }:
                continue
            eligible.append(inv)
        if len(eligible) < required_per_exam:
            return None
        sessions.append((cid, tid, eligible))

    sessions.sort(key=lambda session: (
        len(session[2]), slot_by_id[session[1]]["date"],
        slot_by_id[session[1]]["start_time"], session[0]
    ))
    assigned_count = defaultdict(int)
    assigned_slots = defaultdict(list)
    plan = {}

    def search(index):
        if index == len(sessions):
            return True
        cid, tid, candidates = sessions[index]
        eligible = [
            inv for inv in candidates
            if assigned_count[inv["invigilator_id"]] <
            int(inv.get("max_duties") or 999999) and
            not any(overlaps(slot_by_id[tid], slot_by_id[old_tid])
                    for old_tid in assigned_slots[inv["invigilator_id"]])
        ]
        if len(eligible) < required_per_exam:
            return False
        eligible.sort(key=lambda inv: (
            assigned_count[inv["invigilator_id"]],
            inv.get("department", ""), inv["invigilator_id"]
        ))
        for chosen in combinations(eligible, required_per_exam):
            plan[(cid, tid)] = chosen
            for inv in chosen:
                iid = inv["invigilator_id"]
                assigned_count[iid] += 1
                assigned_slots[iid].append(tid)
            if search(index + 1):
                return True
            for inv in chosen:
                iid = inv["invigilator_id"]
                assigned_count[iid] -= 1
                assigned_slots[iid].pop()
            del plan[(cid, tid)]
        return False

    return plan if search(0) else None


def load_data():
    students = read_csv("students.csv")
    courses = read_csv("courses.csv")
    registrations = read_csv("registrations.csv")
    slots = read_csv("slots.csv")
    venues = read_csv("venues.csv")
    invigilators = read_csv("invigilators.csv")
    availability_rows = read_csv("availability.csv")

    course_by_id = {r["course_id"]: r for r in courses}
    slot_by_id = {r["slot_id"]: r for r in slots}
    venue_by_id = {r["venue_id"]: r for r in venues}
    student_courses = defaultdict(set)
    course_students = defaultdict(set)

    known_students = {r["student_id"] for r in students}
    known_courses = set(course_by_id)
    for r in registrations:
        sid, cid = r["student_id"], r["course_id"]
        if sid not in known_students or cid not in known_courses:
            raise ValueError(f"Invalid registration: student={sid}, course={cid}")
        student_courses[sid].add(cid)
        course_students[cid].add(sid)

    availability = {}
    for r in availability_rows:
        availability[(r["invigilator_id"], r["slot_id"])] = (
            r["available"].strip().lower() in {"1", "true", "yes", "y"}
        )
    return {
        "students": students, "courses": courses, "registrations": registrations,
        "slots": slots, "venues": venues, "invigilators": invigilators,
        "course_by_id": course_by_id, "slot_by_id": slot_by_id,
        "venue_by_id": venue_by_id, "student_courses": student_courses,
        "course_students": course_students, "availability": availability,
    }


def build_conflict_graph(data):
    graph = {c["course_id"]: set() for c in data["courses"]}
    shared_count = {}
    student_clashes = []
    for sid, enrolled in data["student_courses"].items():
        ordered = sorted(enrolled)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                graph[a].add(b)
                graph[b].add(a)
                shared_count[(a, b)] = shared_count.get((a, b), 0) + 1
                student_clashes.append({
                    "student_id": sid, "course_a": a, "course_b": b,
                    "clash_type": "potential_same-slot conflict"
                })
    edges = []
    for a in sorted(graph):
        for b in sorted(graph[a]):
            if a < b:
                edges.append({
                    "course_a": a, "course_b": b,
                    "shared_students": shared_count.get((a, b), 0)
                })
    write_csv("course_conflict_edges.csv", edges,
              ["course_a", "course_b", "shared_students"])
    write_csv("student_conflict_pairs.csv", student_clashes,
              ["student_id", "course_a", "course_b", "clash_type"])
    return graph


def schedule_courses(data, graph, required_per_exam=2):
    """
    DSATUR-style backtracking graph colouring.
    A colour is a slot_id. Courses are assigned only to slots long enough
    for their duration. The search tries to minimise the number of slots used.
    """
    courses = data["course_by_id"]
    slots = data["slots"]
    slot_duration = {}
    for s in slots:
        # HH:MM duration; handles ordinary same-day exam sessions.
        sh, sm = map(int, s["start_time"].split(":"))
        eh, em = map(int, s["end_time"].split(":"))
        slot_duration[s["slot_id"]] = (eh * 60 + em) - (sh * 60 + sm)

    eligible_slots = {
        cid: [s["slot_id"] for s in slots
              if slot_duration[s["slot_id"]] >= int(courses[cid]["duration_minutes"])]
        for cid in courses
    }
    impossible = [cid for cid, choices in eligible_slots.items() if not choices]
    if impossible:
        raise ValueError("No sufficiently long time slot for courses: " + ", ".join(impossible))

    # Try increasing slot-count limits, seeking a feasible colouring.
    ordered_slots = [s["slot_id"] for s in slots]
    assignment = {}
    fallback_solution = None

    def search(limit):
        nonlocal fallback_solution
        if len(assignment) == len(courses):
            if find_invigilator_plan(data, assignment, required_per_exam) is not None:
                return True
            if fallback_solution is None:
                fallback_solution = dict(assignment)
            return False
        unassigned = [cid for cid in courses if cid not in assignment]
        # DSATUR: choose the course with the most distinct neighbour colours,
        # then degree; tie-break deterministically by course ID.
        def key(cid):
            saturation = len({assignment[n] for n in graph[cid] if n in assignment})
            return (saturation, len(graph[cid]), cid)
        cid = max(unassigned, key=key)
        forbidden = [data["slot_by_id"][assignment[n]]
                     for n in graph[cid] if n in assignment]
        used = set(assignment.values())
        candidates = [t for t in eligible_slots[cid]
                      if not any(overlaps(data["slot_by_id"][t], slot)
                                 for slot in forbidden) and
                      (t in used or len(used) < limit)]
        for t in candidates:
            assignment[cid] = t
            if search(limit):
                return True
            del assignment[cid]
        return False

    start = time.perf_counter()
    solution = None
    for limit in range(1, len(ordered_slots) + 1):
        assignment.clear()
        if search(limit):
            solution = dict(assignment)
            break
    if solution is None:
        solution = fallback_solution
    runtime = time.perf_counter() - start
    if solution is None:
        raise ValueError("No feasible timetable found with supplied time slots.")

    # Assign distinct rooms to sessions that overlap in time.
    venue_assignment = {}
    slot_by_id = data["slot_by_id"]
    ordered_courses = sorted(
        solution.items(),
        key=lambda item: (slot_by_id[item[1]]["date"],
                          slot_by_id[item[1]]["start_time"], item[0])
    )
    for cid, tid in ordered_courses:
        required = len(data["course_students"][cid])
        used_venues = {
            venue_assignment[other_cid]
            for other_cid, other_tid in solution.items()
            if other_cid in venue_assignment and
            overlaps(slot_by_id[tid], slot_by_id[other_tid])
        }
        candidates = sorted(
            [v for v in data["venues"]
             if int(v["capacity"]) >= required and v["venue_id"] not in used_venues],
            key=lambda v: (int(v["capacity"]), v["venue_id"])
        )
        if not candidates:
            raise ValueError(
                f"No available venue with capacity {required} for course {cid} in slot {tid}"
            )
        venue_assignment[cid] = candidates[0]["venue_id"]

    rows = []
    for cid, tid in solution.items():
        c = courses[cid]
        s = data["slot_by_id"][tid]
        v = data["venue_by_id"][venue_assignment[cid]]
        rows.append({
            "course_id": cid, "course_code": c["course_code"],
            "course_title": c["course_title"], "slot_id": tid,
            "date": s["date"], "start_time": s["start_time"],
            "end_time": s["end_time"], "venue_id": v["venue_id"],
            "venue_name": v["venue_name"],
            "student_count": len(data["course_students"][cid])
        })
    rows.sort(key=lambda r: (r["date"], r["start_time"], r["course_code"]))
    write_csv("examination_timetable.csv", rows, [
        "course_id", "course_code", "course_title", "slot_id", "date",
        "start_time", "end_time", "venue_id", "venue_name", "student_count"
    ])
    return solution, venue_assignment, runtime, rows


def allocate_invigilators(data, course_slots, timetable_rows, required_per_exam=2):
    invs = data["invigilators"]
    slot_by_id = data["slot_by_id"]
    courses = data["course_by_id"]
    assigned_count = defaultdict(int)
    assigned_slots = defaultdict(list)
    assignments = []
    failures = []


    # Schedule the hardest-to-staff sessions first.
    sessions = sorted(timetable_rows, key=lambda r: (
        sum(1 for i in invs if (
            (i["invigilator_id"], r["slot_id"]) not in data["availability"] or
            data["availability"][(i["invigilator_id"], r["slot_id"])]
        )),
        r["date"], r["start_time"], r["course_id"]
    ))
    staffing_plan = find_invigilator_plan(data, course_slots, required_per_exam)

    for session in sessions:
        cid, tid = session["course_id"], session["slot_id"]
        if staffing_plan is not None:
            chosen = staffing_plan[(cid, tid)]
        else:
            eligible = []
            for inv in invs:
                iid = inv["invigilator_id"]
                available = data["availability"].get((iid, tid), True)
                max_duties = int(inv.get("max_duties") or 999999)
                qualified_raw = (inv.get("qualified_courses") or "*").strip()
                qualified = qualified_raw == "*" or cid in {
                    x.strip() for x in qualified_raw.split(";") if x.strip()
                }
                overlaps_existing = any(
                    overlaps(slot_by_id[tid], slot_by_id[old_tid])
                    for old_tid in assigned_slots[iid]
                )
                if (available and qualified and not overlaps_existing and
                        assigned_count[iid] < max_duties):
                    eligible.append(inv)

            eligible.sort(key=lambda inv: (
                assigned_count[inv["invigilator_id"]],
                inv.get("department", ""), inv["invigilator_id"]
            ))
            chosen = eligible[:required_per_exam]
        if len(chosen) < required_per_exam:
            failures.append({
                "course_id": cid, "slot_id": tid,
                "required": required_per_exam, "available_eligible": len(chosen),
                "message": "Insufficient eligible, available invigilators"
            })
        for inv in chosen:
            iid = inv["invigilator_id"]
            assigned_count[iid] += 1
            assigned_slots[iid].append(tid)
            assignments.append({
                "course_id": cid, "course_code": courses[cid]["course_code"],
                "slot_id": tid, "date": session["date"],
                "start_time": session["start_time"], "end_time": session["end_time"],
                "venue_name": session["venue_name"],
                "invigilator_id": iid, "invigilator_name": inv["name"],
                "duty_number": assigned_count[iid]
            })

    write_csv("invigilator_roster.csv", assignments, [
        "course_id", "course_code", "slot_id", "date", "start_time", "end_time",
        "venue_name", "invigilator_id", "invigilator_name", "duty_number"
    ])
    write_csv("invigilator_allocation_warnings.csv", failures, [
        "course_id", "slot_id", "required", "available_eligible", "message"
    ])
    workload = [
        {"invigilator_id": inv["invigilator_id"], "name": inv["name"],
         "duties_assigned": assigned_count[inv["invigilator_id"]]}
        for inv in invs
    ]
    write_csv("invigilator_workload.csv", workload,
              ["invigilator_id", "name", "duties_assigned"])
    return assignments, failures, workload


def validate_student_clashes(data, course_slots):
    clashes = []
    for sid, enrolled in data["student_courses"].items():
        scheduled = sorted(cid for cid in enrolled if cid in course_slots)
        for index, course_a in enumerate(scheduled):
            slot_a = data["slot_by_id"][course_slots[course_a]]
            for course_b in scheduled[index + 1:]:
                slot_b = data["slot_by_id"][course_slots[course_b]]
                if overlaps(slot_a, slot_b):
                    clashes.append({
                        "student_id": sid, "course_a": course_a,
                        "slot_a": course_slots[course_a], "course_b": course_b,
                        "slot_b": course_slots[course_b]
                    })
    write_csv("validation_student_clashes.csv", clashes,
              ["student_id", "course_a", "slot_a", "course_b", "slot_b"])
    return clashes


def main():
    OUT.mkdir(exist_ok=True)
    data = load_data()
    graph = build_conflict_graph(data)
    course_slots, venue_assignment, runtime, timetable = schedule_courses(data, graph)
    student_clashes = validate_student_clashes(data, course_slots)
    assignments, failures, workload = allocate_invigilators(
        data, course_slots, timetable, required_per_exam=2
    )
    report = {
        "courses_scheduled": len(course_slots),
        "conflict_edges": sum(len(v) for v in graph.values()) // 2,
        "student_clashes_after_scheduling": len(student_clashes),
        "invigilator_assignments": len(assignments),
        "sessions_with_staffing_warnings": len(failures),
        "timetable_solver_runtime_seconds": round(runtime, 4),
        "status": "FEASIBLE_WITH_WARNINGS" if failures else "SUCCESS"
    }
    (OUT / "performance_summary.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    print(f"\nFiles written to: {OUT.resolve()}")
    if student_clashes:
        print("WARNING: Student clashes remain; inspect validation_student_clashes.csv")
    if failures:
        print("WARNING: Some sessions need more eligible invigilators; inspect allocation warnings.")


if __name__ == "__main__":
    main()
