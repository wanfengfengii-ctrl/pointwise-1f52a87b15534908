"""Regression coverage for first-pulse pre-capture tickets."""
from dataclasses import replace

from archive.publish import PublicationInterrupted, receive_and_publish
from capture.pre_capture import arm, collect
from capture.register import OrderConflict, initialize_station, register
from device.pulses import PulseIdentity
from device.segments import list_recorded_segments
from spec.versions import configure
from storage import open_database
from tests.support import StationCase


class PreCaptureTests(StationCase):
    ticket = 'first-pass'
    threshold = 10

    def arm_ticket(self, ticket=None, recorder=None, threshold=None, profile=None):
        return arm(
            self.station, self.device if recorder is None else recorder,
            ticket or self.ticket, (profile or self.profile).key,
            self.threshold if threshold is None else threshold, shift=self.shift
        )

    def test_unknown_and_confirmation_states(self):
        self.assertEqual(collect(self.station, None, self.ticket).status, 'unknown')
        view = arm(self.station, None, self.ticket, self.profile.key,
                   self.threshold, shift=self.shift)
        self.assertEqual(view.status, 'pending_confirmation')
        self.assertIsNone(view.signed)
        self.assertEqual(collect(self.station, None, self.ticket).status,
                         'pending_confirmation')
        self.assertEqual(self.arm_ticket().status, 'pending_pulse')
        self.assertEqual(collect(self.station, None, self.ticket).status, 'pending_pulse')
        again = arm(self.station, None, self.ticket, self.profile.key,
                    self.threshold, shift=self.shift)
        self.assertEqual(again.status, 'pending_pulse')

    def test_threshold_and_parameters_must_be_valid(self):
        for bad in (True, False, 10.0, '10', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                arm(self.station, self.device, 'bad-threshold', self.profile.key,
                    bad, shift=self.shift)
        with self.assertRaises(ValueError):
            arm(self.station, self.device, 'missing-profile', ('fan-line', 'nope'),
                self.threshold, shift=self.shift)

    def test_same_ticket_parameters_are_fixed(self):
        self.arm_ticket()
        arm(self.station, self.device, self.ticket, self.profile.key,
            self.threshold, shift=self.shift)
        for key, threshold, shift in (
            (self.profile.key, self.threshold + 1, self.shift),
            (self.profile.key, self.threshold, 'night-B'),
            (('fan-line', 'other'), self.threshold, self.shift),
        ):
            with self.subTest(params=(key, threshold, shift)), self.assertRaises(OrderConflict):
                arm(self.station, self.device, self.ticket, key, threshold, shift=shift)

    def test_ordinary_registration_cannot_take_the_number(self):
        self.arm_ticket()
        self.trigger(self.q)
        self.record(self.q, 1, 13)
        self.record(self.q, 2, 29)
        with self.assertRaises(OrderConflict):
            register(self.station, self.device, self.ticket, self.q, 2,
                     self.profile.key, shift=self.shift)
        segments = list_recorded_segments(self.device, self.q)
        try:
            publication = receive_and_publish(self.station, self.ticket, segments)
        except (KeyError, OrderConflict):
            pass
        else:
            self.assertIsNone(publication.signed)

    def test_first_qualifying_piece_survives_overwrite_and_stop_source(self):
        intruder = PulseIdentity('recorder-1', 'I')
        self.arm_ticket()
        self.trigger(intruder)
        self.record(intruder, 1, 9)
        self.trigger(self.p)  # retires incomplete I under the original rules
        with self.assertRaises(ValueError):
            self.record(intruder, 2, 41)
        self.record(self.p, 1, 7)
        self.record(self.p, 2, 11)   # completes first, peak 11
        self.trigger(self.q)         # overwrites the slot; P guard keeps originals
        self.record(self.q, 1, 13)
        self.record(self.q, 2, 29)   # louder competitor

        with self.assertRaises(PublicationInterrupted):
            collect(self.station, self.device, self.ticket, interrupt_publication=True)
        self.assertEqual(collect(self.station, None, self.ticket).status, 'pending_pulse')

        self.reopen()
        view = collect(self.station, self.device, self.ticket)
        self.assertEqual(view.status, 'signed')
        self.assertEqual(view.signed.pulse, self.p)
        self.assertEqual(view.signed.measurement.readings_kpa, (7, 11))
        self.assertEqual(view.signed.measurement.peak_kpa, 11)
        receipt = view.signed.receipt
        self.assertTrue(receipt.receipt_id)
        self.assertEqual(receipt.pulse, self.p)
        self.assertTrue(view.reconciliation.consistent)

        with self.assertRaises(OrderConflict):
            self.arm_ticket(threshold=self.threshold + 1)

        # Stop the device source; station-only retrieval returns the same piece.
        self.close_stores()
        self.station = open_database(self.root / 'station.sqlite3')
        initialize_station(self.station)
        offline = collect(self.station, None, self.ticket)
        self.assertEqual(offline.signed, view.signed)
        self.assertEqual(offline.signed.receipt, receipt)

    def _seed_two_pulses(self, q_first):
        self.arm_ticket('ticket-x')
        self.trigger(self.p)
        self.record(self.p, 1, 7)
        register(self.station, self.device, 'ordinary-P', self.p, 2,
                 self.profile.key, shift=self.shift)
        self.trigger(self.q)
        self.record(self.q, 1, 13)
        if q_first:
            self.record(self.q, 2, 29)
            self.record(self.p, 2, 11)
            return self.q
        self.record(self.p, 2, 11)
        self.record(self.q, 2, 29)
        return self.p

    def test_p_first_completion_order(self):
        expected = self._seed_two_pulses(False)
        view = collect(self.station, self.device, 'ticket-x')
        self.assertEqual(view.status, 'signed')
        self.assertEqual(view.signed.pulse, expected)
        self.assertEqual(view.signed.measurement.readings_kpa, (7, 11))

    def test_q_first_completion_order(self):
        expected = self._seed_two_pulses(True)
        view = collect(self.station, self.device, 'ticket-x')
        self.assertEqual(view.status, 'signed')
        self.assertEqual(view.signed.pulse, expected)
        self.assertEqual(view.signed.measurement.readings_kpa, (13, 29))

    def test_affine_eligibility_and_equality_threshold(self):
        affine = configure(self.station, replace(
            self.profile, revision='affine', slope=-2, intercept=20
        ))
        ticket = 'affine-ticket'
        # Station-side pending order exists before the early trigger...
        arm(self.station, None, ticket, affine.key, 10, shift=self.shift)
        early = PulseIdentity('recorder-1', 'T')
        self.trigger(early, profile=affine)
        self.record(early, 1, 2)
        # ...device confirmation fixes the boundary after T was triggered.
        arm(self.station, self.device, ticket, affine.key, 10, shift=self.shift)
        self.record(early, 2, 3)  # completes after arm but triggered before it

        low = PulseIdentity('recorder-1', 'L')
        self.trigger(low, profile=affine)
        self.record(low, 1, 6)
        self.record(low, 2, 8)  # peak 8 below threshold

        winner = PulseIdentity('recorder-1', 'E')
        self.trigger(winner, profile=affine)
        self.assertIsNone(self.record(winner, 2, None))
        self.record(winner, 1, 5)
        self.assertEqual(collect(self.station, self.device, ticket).status, 'pending_pulse')
        self.record(winner, 2, 8)  # [10, 4], peak exactly 10
        view = collect(self.station, self.device, ticket)
        self.assertEqual(view.signed.pulse, winner)
        self.assertEqual(view.signed.measurement.readings_kpa, (10, 4))
        self.assertEqual(view.signed.measurement.peak_kpa, 10)
