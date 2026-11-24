"""Replay the public pressure recorder scenarios against V1 or the new API."""
import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.publish import PublicationInterrupted, receive_and_publish
from capture.register import OrderConflict, initialize_station, register
from capture.resume import resume
from device.pulses import PulseIdentity
from device.readout import read_retained_segments
from device.segments import SegmentOrigin, list_recorded_segments, record_segment
from device.triggers import initialize_device, trigger_pulse
from spec.initialize import DEFAULT_PROFILES
from spec.versions import configure
from storage import open_database


SAMPLE = Path(__file__).resolve().with_name('example') / 'first_pulse.json'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def identity(sample, name):
    return PulseIdentity(sample['recorder'], name)


def origin(sample, name, position):
    return SegmentOrigin(identity(sample, name), position, sample['slot'], f'{name}/seg-{position}')


def replay(recorder, sample, profile, events, station=None):
    for action, name, *args in events:
        pulse = identity(sample, name)
        if action == 'trigger':
            trigger_pulse(recorder, pulse, sample['slot'], profile)
        elif action == 'other_profile':
            trigger_pulse(recorder, pulse, sample['slot'], replace(profile, revision='other'))
        elif action == 'hold':
            require(station is not None, 'ordinary hold in disconnected part of fixture')
            register(station, recorder, 'ordinary-' + name, pulse, profile.segment_count,
                     profile.key, shift=sample['shift'])
        elif action in ('write', 'retired'):
            position, code = args
            source = origin(sample, name, position)
            if action == 'write':
                record_segment(recorder, pulse, position, code, source)
            else:
                try:
                    record_segment(recorder, pulse, position, code, source)
                except ValueError:
                    continue
                raise AssertionError(f'retired pulse {name} accepted a late write')
        else:
            raise ValueError(f'unknown fixture event {action!r}')


def baseline(sample, root):
    profile = DEFAULT_PROFILES[0]
    station_path = root / 'late-station.sqlite3'
    device_path = root / 'late-device.sqlite3'
    with closing(open_database(station_path)) as station:
        initialize_station(station)
    with closing(open_database(device_path)) as recorder:
        initialize_device(recorder)
        replay(recorder, sample, profile, sample['cases']['reuse']['offline'])
    with closing(open_database(station_path)) as station, closing(open_database(device_path)) as recorder:
        p, q = (identity(sample, name) for name in ('P', 'Q'))
        p_rows = list_recorded_segments(recorder, p)
        late = register(station, recorder, 'late-P', p, profile.segment_count,
                        profile.key, shift=sample['shift'])
        p_view = resume(station, recorder, 'late-P')
        register(station, recorder, 'late-Q', q, profile.segment_count,
                 profile.key, shift=sample['shift'])
        q_view = resume(station, recorder, 'late-Q')
        require(not p_rows and late.order.custody == 'unavailable' and p_view.status == 'pending',
                'V1 should retire unheld P at the next trigger')
        require(q_view.status == 'signed' and q_view.signed.measurement.peak_kpa == 29,
                'V1 should still sign the active Q')
    snapshots = []
    for name in ('p_first', 'q_first'):
        case = sample['cases'][name]
        with (
            closing(open_database(root / (name + '-station.sqlite3'))) as station,
            closing(open_database(root / (name + '-device.sqlite3'))) as recorder,
        ):
            initialize_station(station)
            initialize_device(recorder)
            replay(recorder, sample, profile, case['online'], station)
            q = identity(sample, 'Q')
            register(station, recorder, 'ordinary-Q', q, profile.segment_count,
                     profile.key, shift=sample['shift'])
            replay(recorder, sample, profile, case['offline'])
            snapshots.append(tuple(read_retained_segments(recorder, 'capture/ordinary-' + pulse,
                                                          identity(sample, pulse))
                                   for pulse in ('P', 'Q')))
    require(snapshots[0] == snapshots[1] and all(len(r.segments) == 2 for r in snapshots[0]),
            'V1 retained readouts should agree across the two write orders')
    print('V1_PRECAPTURE_EVIDENCE ' + json.dumps({
        'late_P': p_view.status, 'P_raw_segments': len(p_rows), 'P_custody': late.order.custody,
        'late_Q_peak_kpa': q_view.signed.measurement.peak_kpa, 'old_readouts_equal': True,
    }, sort_keys=True))


def check_old_entry(station, recorder, sample, profile):
    ticket = sample['ticket']
    q = identity(sample, 'Q')
    segments = list_recorded_segments(recorder, q)
    require(len(segments) == profile.segment_count, 'Q must be a complete competing payload')
    try:
        register(station, recorder, ticket, q, profile.segment_count, profile.key,
                 shift=sample['shift'])
    except OrderConflict:
        pass
    else:
        raise AssertionError('ordinary registration took over a pre-capture ticket')
    try:
        publication = receive_and_publish(station, ticket, segments)
    except (KeyError, OrderConflict):
        return
    require(publication.signed is None, 'standalone delivery published a competing pulse')


def summarize(view, sample, case, profile):
    name, raw, pressure, peak = case['expected']
    ticket = sample['ticket']
    require(view.order_id == ticket and view.status == 'signed' and view.signed is not None,
            f'{ticket} must be signed')
    result = view.signed
    pulse = identity(sample, name)
    rows = result.measurement.segments
    expected_origins = [origin(sample, name, i) for i in range(1, len(raw) + 1)]
    require(result.order_id == ticket and result.pulse == pulse and result.profile_key == profile.key,
            'wrong original pulse or fixed profile')
    require([row.position for row in rows] == list(range(1, len(raw) + 1)), 'wrong segment order')
    require([row.raw_code for row in rows] == raw and [row.origin for row in rows] == expected_origins,
            'wrong original codes or provenance')
    require(list(result.measurement.readings_kpa) == pressure and result.measurement.peak_kpa == peak,
            'wrong readings or peak')
    receipt = result.receipt
    require(receipt.order_id == ticket and receipt.pulse == pulse and bool(receipt.receipt_id),
            'invalid first receipt')
    return {
        'ticket': ticket, 'pulse': list(result.pulse.key), 'profile': list(result.profile_key),
        'raw_codes': [row.raw_code for row in rows], 'pressures_kpa': list(result.measurement.readings_kpa),
        'peak_kpa': result.measurement.peak_kpa, 'receipt': receipt.receipt_id,
        'sources': [{'position': row.position, 'slot': row.origin.slot, 'source': row.origin.source}
                    for row in rows],
    }


def verify_case(sample, case_name, root):
    from capture.pre_capture import arm, collect

    case = sample['cases'][case_name]
    profile = DEFAULT_PROFILES[0]
    if 'calibration' in case:
        slope, intercept = case['calibration']
        profile = replace(profile, revision='affine', slope=slope, intercept=intercept)
    station_path = root / 'station.sqlite3'
    device_path = root / 'device.sqlite3'
    ticket = sample['ticket']
    threshold = sample['threshold_kpa']
    shift = sample['shift']
    with closing(open_database(device_path)) as recorder:
        initialize_device(recorder)
        with closing(open_database(station_path)) as station:
            initialize_station(station)
            configure(station, profile)
            replay(recorder, sample, profile, case.get('before_arm', ()), station)
            require(collect(station, None, ticket).status == 'unknown', 'unregistered ticket')
            require(arm(station, None, ticket, profile.key, threshold, shift=shift).status
                    == 'pending_confirmation', 'arming without a device must remain pending')
            require(arm(station, recorder, ticket, profile.key, threshold, shift=shift).status
                    == 'pending_pulse', 'device should be armed before the offline part')
            replay(recorder, sample, profile, case.get('online', ()), station)
        replay(recorder, sample, profile, case['offline'])

    if case_name == 'reuse':
        with closing(open_database(station_path)) as station, closing(open_database(device_path)) as recorder:
            armed_again = arm(station, recorder, ticket, profile.key, threshold, shift=shift)
            require(armed_again.status == 'pending_pulse', 'an identical arm should remain in progress')
            check_old_entry(station, recorder, sample, profile)
            try:
                collect(station, recorder, ticket, interrupt_publication=True)
            except PublicationInterrupted:
                pass
            else:
                raise AssertionError('the signing interruption was not exercised')
            pending = collect(station, None, ticket)
            require(pending.status == 'pending_pulse' and pending.signed is None,
                    'signing interruption exposed a result or receipt')

    with closing(open_database(station_path)) as station, closing(open_database(device_path)) as recorder:
        if case_name in ('p_first', 'q_first'):
            ordinary = resume(station, recorder, 'ordinary-P')
            require(ordinary.status == 'signed' and ordinary.signed.measurement.readings_kpa == (7, 11),
                    'the ordinary hold must remain independently usable')
        collect_view = collect(station, recorder, ticket)
        live = summarize(collect_view, sample, case, profile)
        replayed = arm(station, recorder, ticket, profile.key, threshold, shift=shift)
        require(replayed.signed == collect_view.signed, 'same arm parameters changed the result')
        try:
            arm(station, recorder, ticket, profile.key, threshold + 1, shift=shift)
        except OrderConflict:
            pass
        else:
            raise AssertionError('a changed threshold was accepted under the original ticket')

    with closing(open_database(station_path)) as station:
        offline = summarize(collect(station, None, ticket), sample, case, profile)
    require(live == offline, 'offline replay changed the issued result or receipt')
    return offline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline', 'verify', 'demo'))
    mode = parser.parse_args().mode
    sample = json.loads(SAMPLE.read_text(encoding='utf-8'))
    if mode == 'baseline':
        with TemporaryDirectory() as directory:
            baseline(sample, Path(directory))
        return 0
    if mode == 'verify':
        from check_project import main as original_checks
        status = original_checks()
        if status:
            return status
    names = tuple(sample['cases']) if mode == 'verify' else ('reuse',)
    for name in names:
        with TemporaryDirectory() as directory:
            result = verify_case(sample, name, Path(directory))
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'first_pulse/' + name, 'input': sample['cases'][name],
            'actual': result, 'downstream': 'closed device and reopened the station database',
        }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
