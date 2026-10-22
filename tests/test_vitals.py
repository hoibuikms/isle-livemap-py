"""Ports of UnrealDinosaurVitalsTrackerTests.cs (recorded inbound Iris packets)."""

from __future__ import annotations

import pytest

from isle_livemap.decode import parse_iris_packet
from isle_livemap.tracking import VitalsTracker, decode_detached_attribute_frame

T0 = 1_000.0


def test_malformed_packet_does_not_block_following_valid_packets(b64):
    tr = VitalsTracker()
    assert tr.track(bytes([0xFF, 0x00, 0x13]), T0) is None
    assert tr.track(b64("FirstHeartbeat"), T0 + 0.1) is None
    assert tr.track(b64("SecondHeartbeat"), T0 + 1.1) is not None


def test_learns_owner_and_reads_gas_attributes(b64):
    tr = VitalsTracker()
    assert tr.track(b64("FirstHeartbeat"), T0) is None
    hb = tr.track(b64("SecondHeartbeat"), T0 + 1.056)
    assert hb is not None
    assert hb.net_ref_handle == 103_940
    assert hb.vitals.hunger == 0.2961587607860565
    assert hb.vitals.thirst == 837.140869140625
    assert hb.vitals.health is None
    full = tr.track(b64("PeriodicAttributes"), T0 + 2)
    assert full is not None
    v = full.vitals
    assert full.net_ref_handle == 103_940
    assert v.growth == 0.10464542359113693
    assert v.health == 4.601197242736816
    assert v.max_health == 4.601197242736816
    assert v.stamina == 175.84938049316406
    assert v.max_stamina == 175.84938049316406
    assert v.max_hunger == 1.518395185470581
    assert v.max_thirst == 1000.0


def test_reacquires_owner_after_heartbeat_timeout(b64):
    tr = VitalsTracker()
    tr.track(b64("FirstHeartbeat"), T0)
    assert tr.track(b64("SecondHeartbeat"), T0 + 1) is not None
    assert tr.track(b64("FirstHeartbeat"), T0 + 20) is None
    again = tr.track(b64("SecondHeartbeat"), T0 + 21)
    assert again is not None and again.net_ref_handle == 103_940


def test_bootstraps_trailing_health_and_stamina_before_maximum_frame(b64):
    tr = VitalsTracker()
    assert tr.track(b64("TrailingAttributesHeartbeat1"), T0) is None
    o = tr.track(b64("TrailingAttributesHeartbeat2"), T0 + 1.03)
    assert o is not None
    assert o.net_ref_handle == 208_310
    assert o.vitals.hunger == 0.4000110626220703
    assert o.vitals.thirst == 990.9522705078125
    assert o.vitals.health == 1.0151753425598145
    assert o.vitals.stamina == 91.7905044555664
    assert o.vitals.max_health is None and o.vitals.max_stamina is None


def test_rejects_pteranodon_false_health_but_keeps_stamina(b64):
    tr = VitalsTracker()
    assert tr.track(b64("PteranodonHeartbeat1"), T0) is None
    o = tr.track(b64("PteranodonHeartbeat2"), T0 + 1)
    assert o is not None
    assert o.net_ref_handle == 54_816
    assert o.vitals.health is None
    assert o.vitals.stamina == 277.3397521972656


def test_sparse_maximum_frame_and_restore_after_gap(b64):
    tr = VitalsTracker()
    assert tr.track(b64("SparseMaximumHeartbeat1"), T0) is None
    assert tr.track(b64("SparseMaximumHeartbeat2"), T0 + 1) is not None
    c = tr.track(b64("SparseMaximumAttributes"), T0 + 2)
    assert c is not None
    v = c.vitals
    assert c.net_ref_handle == 54_816
    assert v.growth == 0.711446225643158
    assert v.health == 62.79351043701172 and v.max_health == 62.79351043701172
    assert v.stamina == 777.4014282226562 and v.max_stamina == 777.4014282226562
    assert v.max_hunger == 20.721858978271484
    assert tr.track(b64("SparseMaximumHeartbeat1"), T0 + 20) is None
    r = tr.track(b64("SparseMaximumHeartbeat2"), T0 + 21)
    assert r is not None
    assert r.vitals.max_health == 62.79351043701172
    assert r.vitals.max_stamina == 777.4014282226562
    assert r.vitals.max_hunger == 20.721858978271484


def test_reset_drops_verified_maximums_even_if_handle_reused(b64):
    tr = VitalsTracker()
    tr.track(b64("SparseMaximumHeartbeat1"), T0)
    tr.track(b64("SparseMaximumHeartbeat2"), T0 + 1)
    c = tr.track(b64("SparseMaximumAttributes"), T0 + 2)
    assert c is not None and c.vitals.max_health is not None
    tr.reset()
    assert tr.track(b64("SparseMaximumHeartbeat1"), T0 + 3) is None
    r = tr.track(b64("SparseMaximumHeartbeat2"), T0 + 4)
    assert r is not None and r.net_ref_handle == 54_816
    assert r.vitals.max_health is None
    assert r.vitals.max_stamina is None
    assert r.vitals.max_hunger is None


def test_detached_frame_skips_denormal_overlap(b64):
    v = decode_detached_attribute_frame(b64("DetachedDamagedAttributes"), 33.55)
    assert v is not None
    assert v.growth == 0.026307906955480576
    assert v.health == 83.77029418945312
    assert v.max_health == 98.46177673339844
    assert v.stamina == 169.23199462890625
    assert v.max_stamina == 169.23199462890625
    assert v.max_hunger == 49.23088836669922


def test_iris_parser_exposes_batches_on_recorded_packet(b64):
    pkt = parse_iris_packet(b64("SecondHeartbeat"))
    assert pkt is not None and pkt.has_data_stream
    assert any(b.net_ref_handle == 103_940 and b.has_owner_data for b in pkt.batches)


@pytest.mark.parametrize("junk", [b"", b"\x00", b"\xff" * 3, bytes(range(200))])
def test_iris_parser_never_raises_on_junk(junk):
    parse_iris_packet(junk)
