"""First qualifying pulse pre-capture tickets.

``arm`` fixes the threshold/frozen profile before the station library can go
offline; ``collect`` deterministically takes over the first complete pulse
that reaches the threshold and signs it under the ticket.

Selection contract (see FIRST_PULSE.md):

* only pulses actually triggered after the device confirmed the arm count;
* the pulse must use the fixed frozen profile, checked by key and parameters;
* complete pulses are ordered by the device submission instant of their last
  valid original segment;
* the first in that order whose peak under the original calibration reaches
  the integer threshold (equality counts) is the unique signable pulse.

The signed result, first receipt, provenance and selection basis are committed
to the station library together, so closing the device and reopening only the
station still returns the same piece and the same first execution.
"""
from __future__ import annotations

import json

from archive.admit import RejectedSegment, admit_segment
from archive.completeness import complete_segments
from archive.ledger import AdmissionConflict, list_admitted_segments
from archive.measurement import measure_pulse
from archive.publish import PublicationInterrupted
from archive.results import (
    FirstReceipt, SignedResult, find_signed_result, store_first_receipt, store_result,
)
from capture.orders import CaptureOrder, get_order, save_new_order, save_handoff_progress
from capture.pre_orders import (
    PreCaptureOrder, ensure_pre_order_store, get_pre_order, save_pre_order,
    mark_pre_order_confirmed, store_basis,
)
from capture.register import OrderConflict
from capture.release import release_after_publish
from device.pre_capture import guard_request_id, scan_completed_pulses
from device.readout import RetentionNotReadable, read_retained_segments
from device.retention import request_retention
from shift.lookup import LookupResult, lookup
from spec.versions import get_version
from storage import require_idle, transaction


def _validate_profile_key(profile_key):
    if (not isinstance(profile_key, tuple) or len(profile_key) != 2
            or any(not isinstance(part, str) or not part.strip() for part in profile_key)):
        raise ValueError('a line and fixed profile revision are required')


def _status_view(station, ticket):
    signed = find_signed_result(station, ticket)
    if signed is not None:
        return lookup(station, ticket)
    pre = get_pre_order(station, ticket)
    if pre is None:
        return LookupResult(ticket, 'unknown')
    status = 'pending_confirmation' if not pre.confirmed else 'pending_pulse'
    return LookupResult(ticket, status)


def arm(station, recorder, ticket, profile_key, threshold_kpa, *, shift):
    """Register/confirm a pre-capture ticket without signing anything."""
    from capture.orders import validate_order_id, validate_shift

    validate_order_id(ticket)
    validate_shift(shift)
    _validate_profile_key(profile_key)
    # ``bool`` is a subclass of int; the threshold must be a genuine integer.
    if type(threshold_kpa) is not int:
        raise ValueError('threshold_kpa must be an integer kPa value')
    if recorder is not None:
        require_idle(recorder)

    with transaction(station, write=True):
        ensure_pre_order_store(station)
        pre = get_pre_order(station, ticket)
        ordinary = get_order(station, ticket)
        profile = get_version(station, profile_key)
        if pre is None:
            if profile is None:
                raise ValueError('the fixed profile version must be registered before arming')
            if ordinary is not None:
                raise OrderConflict('ticket is already used by an ordinary capture order')
            save_pre_order(station, PreCaptureOrder(
                ticket, profile_key, profile.segment_count, threshold_kpa, shift, False
            ))
        elif (pre.profile_key != profile_key
                or pre.threshold_kpa != threshold_kpa or pre.shift != shift):
            raise OrderConflict('pre-capture ticket is fixed to different parameters')
        elif profile is None:
            raise RuntimeError('pre-capture ticket lost its fixed profile version')

    if recorder is not None:
        # Imported lazily to keep the module graph acyclic.
        from device.triggers import initialize_device

        initialize_device(recorder)
        from device.pre_capture import arm_on_device

        arm_on_device(recorder, ticket, profile)
        with transaction(station, write=True):
            mark_pre_order_confirmed(station, ticket)
        # Parity with ordinary registration: an already signed ticket replays
        # any pending device release instead of re-signing.
        if find_signed_result(station, ticket) is not None:
            release_after_publish(station, recorder, ticket)

    return _status_view(station, ticket)


class _RollbackPending(Exception):
    """Internal: publication is not signable yet, roll the attempt back."""


def _publish_winner(station, pre, profile, candidate, segments, *, interrupt_publication):
    """Atomically bind the ticket to the winner and publish its result.

    Mirrors ``archive.publish.receive_and_publish`` including the interruption
    point between storing the result and storing the first receipt; the whole
    transaction is rolled back on interruption, leaving no result/receipt and
    no competing admission behind.
    """
    ticket = pre.ticket
    guard_id = guard_request_id(ticket, candidate.pulse)

    try:
        with transaction(station, write=True):
            ensure_pre_order_store(station)
            if find_signed_result(station, ticket) is not None:
                return find_signed_result(station, ticket)
            order = get_order(station, ticket)
            if order is None:
                order = CaptureOrder(
                    ticket, candidate.pulse, profile.segment_count, pre.profile_key,
                    pre.shift, guard_id, custody='held'
                )
                save_new_order(station, order)
            elif order.pulse != candidate.pulse:
                # The station basis determines one unique signable pulse; a
                # replayed or competing delivery must never swap it.
                raise OrderConflict('pre-capture ticket is already bound to another pulse')

            rejected = []
            for segment in segments:
                try:
                    admit_segment(station, order, profile, segment)
                except (RejectedSegment, AdmissionConflict) as error:
                    rejected.append((segment.position, str(error)))

            admitted = list_admitted_segments(station, ticket)
            save_handoff_progress(station, ticket, len(admitted))
            ordered = complete_segments(profile, admitted)
            if ordered is None or order.custody != 'held' or rejected:
                raise _RollbackPending()

            measurement = measure_pulse(profile, ordered)
            receipt = FirstReceipt('receipt/' + ticket, ticket, candidate.pulse)
            signed = SignedResult(
                ticket, candidate.pulse, pre.profile_key, measurement, receipt
            )
            store_result(station, signed)
            store_basis(station, ticket, json.dumps({
                'ticket': ticket,
                'pulse': list(candidate.pulse.key),
                'profile_key': list(pre.profile_key),
                'threshold_kpa': pre.threshold_kpa,
                'trigger_rowid': candidate.trigger_rowid,
                'completed_rowid': candidate.completed_rowid,
                'peak_kpa': measurement.peak_kpa,
                'positions': [segment.position for segment in ordered],
            }, sort_keys=True))
            if interrupt_publication:
                raise PublicationInterrupted(
                    'pre-capture archive interrupted between result and first receipt'
                )
            store_first_receipt(station, receipt)
            return signed
    except _RollbackPending:
        return None


def collect(station, recorder, ticket, *, interrupt_publication=False):
    """Take over the first qualifying piece and sign it, or query state."""
    from capture.orders import validate_order_id

    validate_order_id(ticket)
    with transaction(station, write=True):
        ensure_pre_order_store(station)
        pre = get_pre_order(station, ticket)
    if pre is None:
        return LookupResult(ticket, 'unknown')

    if find_signed_result(station, ticket) is not None:
        view = lookup(station, ticket)
        if recorder is not None:
            require_idle(recorder)
            release_after_publish(station, recorder, ticket)
        return view

    if not pre.confirmed:
        return LookupResult(ticket, 'pending_confirmation')
    if recorder is None:
        return LookupResult(ticket, 'pending_pulse')

    require_idle(recorder)
    profile = get_version(station, pre.profile_key)
    scanned = scan_completed_pulses(recorder, ticket)
    if scanned is None or profile is None:
        return LookupResult(ticket, 'pending_pulse')
    _arm, candidates = scanned

    winner = None
    for candidate in candidates:
        # Check the frozen procedure by both version key and parameters.
        if candidate.frozen_profile != profile:
            continue
        measurement = measure_pulse(profile, candidate.segments)
        if measurement.peak_kpa >= pre.threshold_kpa:
            winner = candidate
            break
    if winner is None:
        return LookupResult(ticket, 'pending_pulse')

    guard_id = guard_request_id(ticket, winner.pulse)
    retention = request_retention(recorder, guard_id, winner.pulse)
    if retention is None:
        return LookupResult(ticket, 'pending_pulse')
    try:
        retained = read_retained_segments(recorder, guard_id, winner.pulse)
    except RetentionNotReadable:
        # The device may have been source-stopped or the originals reclaimed;
        # station-only collection still works once a result is committed.
        return LookupResult(ticket, 'pending_pulse')

    signed = _publish_winner(
        station, pre, profile, winner, retained.segments,
        interrupt_publication=interrupt_publication
    )
    if signed is None:
        return LookupResult(ticket, 'pending_pulse')

    # Release the winner guard under the ordinary post-publication convention.
    # Competitor guards are deliberately retained: ordinary work orders and
    # later inspection must not lose their originals through pre-capture.
    release_after_publish(station, recorder, ticket)
    return lookup(station, ticket)
