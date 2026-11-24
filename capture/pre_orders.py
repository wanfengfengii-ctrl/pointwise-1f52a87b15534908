"""Station-side registry for first-pulse pre-capture tickets.

A pre-capture ticket fixes its profile version, pressure threshold and shift
before any candidate pulse is selected.  It is deliberately kept outside
``capture_orders`` until ``collect`` signs a concrete pulse: ordinary
registration and standalone admission therefore cannot bind or publish the
ticket on their own.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PreCaptureOrder:
    ticket: str
    profile_key: tuple[str, str]
    segment_count: int
    threshold_kpa: int
    shift: str
    confirmed: bool = False


def ensure_pre_order_store(connection):
    connection.execute('''
        CREATE TABLE IF NOT EXISTS pre_capture_orders (
            ticket TEXT PRIMARY KEY,
            segment_count TEXT NOT NULL,
            line TEXT NOT NULL,
            revision TEXT NOT NULL,
            threshold_kpa TEXT NOT NULL,
            shift TEXT NOT NULL,
            confirmed INTEGER NOT NULL,
            FOREIGN KEY (line, revision) REFERENCES profile_versions(line, revision)
        )
    ''')
    connection.execute('''
        CREATE INDEX IF NOT EXISTS pre_orders_by_shift
        ON pre_capture_orders (shift, ticket)
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS pre_capture_basis (
            ticket TEXT PRIMARY KEY REFERENCES pre_capture_orders(ticket),
            basis_json TEXT NOT NULL
        )
    ''')


def _order_from_row(row):
    return PreCaptureOrder(
        row['ticket'], (row['line'], row['revision']),
        int(row['segment_count'], 16), int(row['threshold_kpa'], 16),
        row['shift'], bool(row['confirmed'])
    )


def get_pre_order(connection, ticket):
    row = connection.execute('''
        SELECT * FROM pre_capture_orders WHERE ticket = ?
    ''', (ticket,)).fetchone()
    return None if row is None else _order_from_row(row)


def save_pre_order(connection, order):
    connection.execute('''
        INSERT INTO pre_capture_orders
            (ticket, segment_count, line, revision, threshold_kpa, shift, confirmed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        order.ticket, hex(order.segment_count), *order.profile_key,
        hex(order.threshold_kpa), order.shift, 1 if order.confirmed else 0
    ))
    return order


def mark_pre_order_confirmed(connection, ticket):
    connection.execute('''
        UPDATE pre_capture_orders SET confirmed = 1 WHERE ticket = ?
    ''', (ticket,))
    return get_pre_order(connection, ticket)


def store_basis(connection, ticket, basis_json):
    connection.execute('''
        INSERT INTO pre_capture_basis (ticket, basis_json) VALUES (?, ?)
        ON CONFLICT (ticket) DO UPDATE SET basis_json = excluded.basis_json
    ''', (ticket, basis_json))


def get_basis(connection, ticket):
    row = connection.execute('''
        SELECT basis_json FROM pre_capture_basis WHERE ticket = ?
    ''', (ticket,)).fetchone()
    return None if row is None else row['basis_json']
