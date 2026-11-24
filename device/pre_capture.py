"""Device-side support for first-pulse pre-capture.

An *arm* records the fixed frozen profile and the trigger boundary: only
physical pulses whose ``physical_pulses.rowid`` is strictly greater than the
boundary were triggered after the device confirmed the arm.  Re-arming the
same ticket keeps the original boundary.

When a matching pulse becomes complete, the recording entry places a normal
held retention (a *guard*) on it.  Guards use the ``precapture/`` request
prefix because ``capture/`` is reserved for ordinary work orders.  A guard is
what lets the original segments and provenance survive slot overwrite,
ordinary-retention release and device reopen.  Pulses that never complete get
no guard and still retire under the original rules.
"""
from __future__ import annotations

from dataclasses import dataclass

from device.pulses import PulseIdentity
from device.retention import ensure_retention_store
from spec.profile import ProfileVersion
from spec.versions import profile_columns, profile_from_row
from storage import transaction


PRE_CAPTURE_PREFIX = 'precapture/'


@dataclass(frozen=True)
class DeviceArm:
    ticket: str
    profile: ProfileVersion
    boundary_rowid: int


def ensure_device_pre_store(connection):
    connection.execute('''
        CREATE TABLE IF NOT EXISTS precapture_arms (
            ticket TEXT PRIMARY KEY,
            line TEXT NOT NULL,
            revision TEXT NOT NULL,
            segment_count TEXT NOT NULL,
            raw_min TEXT NOT NULL,
            raw_max TEXT NOT NULL,
            slope TEXT NOT NULL,
            intercept TEXT NOT NULL,
            boundary_rowid INTEGER NOT NULL
        )
    ''')


def guard_request_id(ticket, pulse):
    # Length-prefixed components keep the identifier injective even when names
    # contain separators, and the prefix avoids the ordinary ``capture/`` range.
    return (
        PRE_CAPTURE_PREFIX + 'g/'
        + str(len(ticket)) + ':' + ticket + '/'
        + str(len(pulse.recorder)) + ':' + pulse.recorder + '/'
        + str(len(pulse.pulse)) + ':' + pulse.pulse
    )


def _arm_from_row(row):
    return DeviceArm(
        row['ticket'],
        ProfileVersion(
            row['line'], row['revision'], int(row['segment_count'], 16),
            int(row['raw_min'], 16), int(row['raw_max'], 16),
            int(row['slope'], 16), int(row['intercept'], 16)
        ),
        row['boundary_rowid']
    )


def load_device_arm(connection, ticket):
    row = connection.execute('''
        SELECT * FROM precapture_arms WHERE ticket = ?
    ''', (ticket,)).fetchone()
    return None if row is None else _arm_from_row(row)


def arm_on_device(connection, ticket, profile):
    """Idempotently fix the effective trigger boundary for a ticket."""
    profile.validate()
    with transaction(connection, write=True):
        ensure_device_pre_store(connection)
        existing = load_device_arm(connection, ticket)
        if existing is not None:
            if existing.profile != profile:
                raise ValueError('pre-capture device arm is fixed to another frozen profile')
            return existing
        boundary = connection.execute('''
            SELECT COALESCE(MAX(rowid), 0) FROM physical_pulses
        ''').fetchone()[0]
        connection.execute('''
            INSERT INTO precapture_arms
                (ticket, line, revision, segment_count, raw_min, raw_max,
                 slope, intercept, boundary_rowid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (ticket, *profile_columns(profile), boundary))
        return DeviceArm(ticket, profile, boundary)


def _arm_matches_profile(arm_row, profile):
    frozen = ProfileVersion(
        arm_row['line'], arm_row['revision'], int(arm_row['segment_count'], 16),
        int(arm_row['raw_min'], 16), int(arm_row['raw_max'], 16),
        int(arm_row['slope'], 16), int(arm_row['intercept'], 16)
    )
    return frozen == profile


def guards_for_completed_segment(connection, physical, pulse):
    """Place guards when the just-written segment completes an eligible pulse.

    Runs inside the active ``record_segment`` transaction, so every statement
    here shares its commit/rollback.  No-op when no arm exists.
    """
    ensure_device_pre_store(connection)
    arm_rows = connection.execute('SELECT * FROM precapture_arms').fetchall()
    if not arm_rows:
        return
    ensure_retention_store(connection)
    total = connection.execute('''
        SELECT COUNT(*) FROM device_segments WHERE recorder = ? AND pulse = ?
    ''', pulse.key).fetchone()[0]
    if total != physical.profile.segment_count:
        return
    trigger_row = connection.execute('''
        SELECT rowid AS trigger_rowid FROM physical_pulses
        WHERE recorder = ? AND pulse = ?
    ''', pulse.key).fetchone()
    if trigger_row is None:
        return
    for arm_row in arm_rows:
        if trigger_row['trigger_rowid'] <= arm_row['boundary_rowid']:
            continue
        if not _arm_matches_profile(arm_row, physical.profile):
            continue
        request_id = guard_request_id(arm_row['ticket'], pulse)
        present = connection.execute('''
            SELECT 1 FROM retention_requests WHERE request_id = ?
        ''', (request_id,)).fetchone()
        if present is not None:
            continue
        connection.execute('''
            INSERT INTO retention_requests (request_id, recorder, pulse, status)
            VALUES (?, ?, ?, 'held')
        ''', (request_id, *pulse.key))


@dataclass(frozen=True)
class DeviceCandidate:
    pulse: PulseIdentity
    frozen_profile: ProfileVersion
    trigger_rowid: int
    completed_rowid: int
    segments: tuple


def scan_completed_pulses(connection, ticket):
    """Return ``(arm, candidates_in_completion_order)`` or ``None``.

    Candidates are post-boundary complete pulses.  Ordering uses the rowid of
    each pulse's last originally submitted segment; guarded survivors make
    these rowids increase strictly with true completion order even after other
    pulses' rows are retired and deleted.
    """
    # Lazy import: device.segments imports this module's hook.
    from device.segments import list_recorded_segments

    with transaction(connection):
        arm = load_device_arm(connection, ticket)
        if arm is None:
            return None
        rows = connection.execute('''
            SELECT rowid AS trigger_rowid, * FROM physical_pulses
        ''').fetchall()
        candidates = []
        for row in rows:
            if row['trigger_rowid'] <= arm.boundary_rowid:
                continue
            pulse = PulseIdentity(row['recorder'], row['pulse'])
            frozen_profile = profile_from_row(row)
            segments = list_recorded_segments(connection, pulse)
            positions = tuple(segment.position for segment in segments)
            if positions != tuple(range(1, frozen_profile.segment_count + 1)):
                continue
            completed_rowid = connection.execute('''
                SELECT MAX(rowid) FROM device_segments
                WHERE recorder = ? AND pulse = ?
            ''', pulse.key).fetchone()[0]
            candidates.append(DeviceCandidate(
                pulse, frozen_profile, row['trigger_rowid'],
                int(completed_rowid), segments
            ))
        candidates.sort(key=lambda candidate: (
            candidate.completed_rowid, candidate.pulse.recorder, candidate.pulse.pulse
        ))
        return arm, tuple(candidates)
