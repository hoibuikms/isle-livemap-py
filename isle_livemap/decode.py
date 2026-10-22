"""Bit-level decoders for the player's OWN game traffic.

Ported from the open-source (MIT) UnrealMovementPacketDecoder.cs and
UnrealIrisPacketParser.cs of Undo-IsleLiveMap. Everything here is a pure
function of captured bytes: nothing is sent, modified or injected.

Bit order is Unreal's: LSB-first inside each byte, bytes in stream order. That
is exactly the bit order of ``int.from_bytes(payload, "little")``, so a payload
is turned into one big integer and bit fields are read with shift and mask.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------- helpers


def mask(n: int) -> int:
    return (1 << n) - 1


def f32(raw_bits: int) -> float:
    """Reinterpret 32 raw bits as an IEEE-754 single (returned as a double)."""
    return struct.unpack("<f", struct.pack("<I", raw_bits & 0xFFFFFFFF))[0]


def to_f32(value: float) -> float:
    """Round a double to single precision (C# ``float``)."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.inf if value > 0 else -math.inf


class Bits:
    """Random-access bit view over a payload."""

    __slots__ = ("big", "nbits")

    def __init__(self, payload: bytes) -> None:
        self.big = int.from_bytes(payload, "little")
        self.nbits = len(payload) * 8

    def read(self, offset: int, count: int) -> int:
        return (self.big >> offset) & mask(count)

    def is_set(self, offset: int) -> bool:
        return (self.big >> offset) & 1 == 1

    def last_one_before(self, before: int) -> int:
        """Index of the highest set bit below ``before`` (-1 if none)."""
        before = min(before, self.nbits)
        if before <= 0:
            return -1
        return (self.big & mask(before)).bit_length() - 1


# ---------------------------------------------------------------- movement

MAP_HEADING_OFFSET_DEG = 90.0


def normalize_degrees(degrees: float) -> float:
    n = math.fmod(degrees, 360.0)
    return n + 360.0 if n < 0.0 else n


def map_heading_from_unreal_yaw(yaw_degrees: float) -> float:
    """East-based Unreal yaw -> north-based map angle (MapHeading.cs)."""
    return normalize_degrees(yaw_degrees + MAP_HEADING_OFFSET_DEG)


@dataclass(frozen=True, slots=True)
class MovementCandidate:
    x: float
    y: float
    z: float
    unreal_yaw_degrees: float
    client_timestamp: float
    payload_length: int
    location_bit_offset: int
    component_bit_count: int

    @property
    def map_heading_degrees(self) -> float:
        return map_heading_from_unreal_yaw(self.unreal_yaw_degrees)

    @property
    def layout(self) -> tuple[int, int, int]:
        return (self.payload_length, self.location_bit_offset, self.component_bit_count)


_HEADER_BITS = 7
_FLOAT_BITS = 32
_MIN_LOC_BITS = 23
_MIN_COMP_BITS = 1
_MAX_COMP_BITS = 31
_MIN_WORLD_XY, _MAX_WORLD_XY = -800_000.0, 800_000.0
_MIN_WORLD_Z, _MAX_WORLD_Z = -10_000.0, 100_000.0
_MAX_ACCEL = 100_000.0
_MAX_CLIENT_TS = 10_000_000.0


def _read_signed(bits: Bits, offset: int, count: int) -> int:
    value = bits.read(offset, count)
    sign = 1 << (count - 1)
    return (value ^ sign) - sign


def _bits_needed(value: int) -> int:
    # value ^ (value >> 63) in the 64-bit original: ~value for negatives.
    massaged = ~value if value < 0 else value
    return 1 + massaged.bit_length()


def _canonical(x: int, y: int, z: int, component_bits: int) -> bool:
    return max(_bits_needed(x), _bits_needed(y), _bits_needed(z)) == component_bits


def _plausible_world(x: float, y: float, z: float) -> bool:
    return (
        _MIN_WORLD_XY <= x <= _MAX_WORLD_XY
        and _MIN_WORLD_XY <= y <= _MAX_WORLD_XY
        and _MIN_WORLD_Z <= z <= _MAX_WORLD_Z
    )


def _read_move_prefix(bits: Bits, location_offset: int) -> float | None:
    for accel_bits in range(_MIN_COMP_BITS, _MAX_COMP_BITS + 1):
        accel_off = location_offset - _HEADER_BITS - accel_bits * 3
        ts_off = accel_off - _FLOAT_BITS
        if ts_off < 0:
            continue
        header = bits.read(accel_off, _HEADER_BITS)
        if (header & 63) != accel_bits:
            continue
        base = accel_off + _HEADER_BITS
        rx = _read_signed(bits, base, accel_bits)
        ry = _read_signed(bits, base + accel_bits, accel_bits)
        rz = _read_signed(bits, base + accel_bits * 2, accel_bits)
        if not _canonical(rx, ry, rz, accel_bits):
            continue
        scale = 10.0 if header & 64 else 1.0
        if max(abs(rx), abs(ry), abs(rz)) / scale > _MAX_ACCEL:
            continue
        ts = f32(bits.read(ts_off, _FLOAT_BITS))
        if not math.isfinite(ts) or ts <= 0.0 or ts > _MAX_CLIENT_TS:
            continue
        return ts
    return None


def _read_axis(bits: Bits, pos: int) -> tuple[bool, int, float]:
    if pos >= bits.nbits:
        return False, pos, 0.0
    present = bits.read(pos, 1) != 0
    pos += 1
    if not present:
        return True, pos, 0.0
    if pos + 16 > bits.nbits:
        return False, pos, 0.0
    compressed = bits.read(pos, 16)
    return True, pos + 16, compressed * 360.0 / 65536.0


def _read_rotation(bits: Bits, pos: int) -> tuple[bool, float]:
    """Returns (ok, yaw_degrees); pitch/roll are validated but unused."""
    ok, pos, _pitch = _read_axis(bits, pos)
    if not ok:
        return False, 0.0
    ok, pos, yaw = _read_axis(bits, pos)
    if not ok:
        return False, 0.0
    ok, pos, _roll = _read_axis(bits, pos)
    return (ok, yaw) if ok else (False, 0.0)


def decode_movement(payload: bytes) -> list[MovementCandidate]:
    """Find FVector_NetQuantize100 location + control yaw candidates in a
    client->server FCharacterNetworkMoveData payload (UE 5.5)."""
    if not payload:
        return []
    bits = Bits(payload)
    total = bits.nbits
    min_move_bits = _HEADER_BITS + _MIN_LOC_BITS * 3 + 3
    out: list[MovementCandidate] = []
    big = bits.big
    for off in range(0, total - min_move_bits + 1):
        header = (big >> off) & 127
        comp = header & 63
        if not header & 64 or comp < _MIN_LOC_BITS or comp > _MAX_COMP_BITS:
            continue
        if off + _HEADER_BITS + comp * 3 + 3 > total:
            continue
        base = off + _HEADER_BITS
        rx = _read_signed(bits, base, comp)
        ry = _read_signed(bits, base + comp, comp)
        rz = _read_signed(bits, base + comp * 2, comp)
        if not _canonical(rx, ry, rz, comp):
            continue
        x, y, z = rx / 100.0, ry / 100.0, rz / 100.0
        if not _plausible_world(x, y, z):
            continue
        ts = _read_move_prefix(bits, off)
        if ts is None:
            continue
        ok, yaw = _read_rotation(bits, off + _HEADER_BITS + comp * 3)
        if not ok:
            continue
        out.append(MovementCandidate(x, y, z, yaw, ts, len(payload), off, comp))
    return out


# ---------------------------------------------------------------- Iris envelope


@dataclass(frozen=True, slots=True)
class IrisBatch:
    net_ref_handle: int
    data_bit_offset: int
    data_bit_count: int
    has_owner_data: bool
    has_exports: bool


@dataclass(frozen=True, slots=True)
class IrisPacket:
    packet_sequence: int
    has_data_stream: bool
    is_complete: bool
    batches: list[IrisBatch] = field(default_factory=list)
    incomplete_reason: str | None = None
    # First field of each destruction record (an Iris packed u64 in the same domain as ``net_ref_handle``).
    # Read by the existing envelope walk; the semantics are NOT validated against a recorded despawn.
    destroyed_handles: tuple[int, ...] = ()


_MAX_BATCHES = 8192


class _Reader:
    __slots__ = ("big", "limit", "pos")

    def __init__(self, big: int, limit: int, pos: int = 0) -> None:
        self.big = big
        self.limit = limit
        self.pos = pos

    def skip(self, n: int) -> bool:
        if n < 0 or self.pos > self.limit - n:
            return False
        self.pos += n
        return True

    def bits(self, n: int) -> int | None:
        if n < 0 or n > 64 or self.pos > self.limit - n:
            return None
        v = (self.big >> self.pos) & mask(n)
        self.pos += n
        return v

    def serialized_int(self, maximum: int) -> int | None:
        value, m = 0, 1
        while value + m < maximum:
            b = self.bits(1)
            if b is None:
                return None
            if b:
                value |= m
            m <<= 1
        return value

    def legacy_packed_u32(self) -> int | None:
        value = 0
        for shift in range(0, 35, 7):
            cur = self.bits(8)
            if cur is None:
                return None
            value |= (cur >> 1) << shift
            if cur & 1 == 0:
                return value if value <= 0xFFFFFFFF else None
        return None

    def iris_packed_u64(self) -> int | None:
        n = self.bits(3)
        return None if n is None else self.bits((n + 1) * 8)

    def iris_packed_u32(self) -> int | None:
        n = self.bits(2)
        return None if n is None else self.bits((n + 1) * 8)

    def skip_fstring_and_number(self) -> bool:
        raw = self.bits(32)
        if raw is None:
            return False
        length = raw - (1 << 32) if raw >= 1 << 31 else raw
        bit_count = abs(length) * (16 if length < 0 else 8)
        return self.skip(bit_count) and self.skip(32)


def _skip_net_token(r: _Reader, includes_type_id: bool) -> bool:
    token = r.iris_packed_u32()
    if token is None:
        return False
    return token == 0 or (r.skip(1) and (not includes_type_id or r.skip(3)))


def _skip_net_string(r: _Reader) -> bool:
    if not r.skip(1):
        return False
    n = r.bits(16)
    return n is not None and r.skip(n * 8)


def _skip_conditional_net_string(r: _Reader) -> bool:
    has = r.bits(1)
    return has is not None and (has == 0 or _skip_net_string(r))


def _skip_net_ref_handle(r: _Reader) -> bool:
    has = r.bits(1)
    if has is None:
        return False
    if has == 0:
        return True
    h = r.iris_packed_u64()
    return h is not None and h != 0


def _skip_object_reference_body(r: _Reader, depth: int) -> bool:
    if depth > 32:
        return False
    has_ref = r.bits(1)
    if has_ref is None:
        return False
    if has_ref == 0:
        return True
    handle = r.iris_packed_u64()
    if handle is None or handle == 0:
        return False
    has_path = r.bits(1)
    if has_path is None:
        return False
    if has_path == 0:
        return True
    if not r.skip(1):
        return False
    has_outer = r.bits(1)
    if has_outer is None:
        return False
    return has_outer == 0 or (
        _skip_net_token(r, False)
        and _skip_conditional_net_string(r)
        and _skip_object_reference_body(r, depth + 1)
    )


def _skip_inline_full_object_reference(r: _Reader, depth: int) -> bool:
    if depth > 32:
        return False
    exported = r.bits(1)
    if exported is None:
        return False
    if exported:
        return (
            _skip_net_ref_handle(r)
            and _skip_net_token(r, False)
            and _skip_conditional_net_string(r)
        )
    return _skip_object_reference_body(r, depth)


def _skip_exports(r: _Reader) -> bool:
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return False
        if has == 0:
            break
        if not _skip_net_token(r, True) or not _skip_net_string(r):
            return False
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return False
        if has == 0:
            break
        if not _skip_inline_full_object_reference(r, 0):
            return False
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return False
        if has == 0:
            return True
        has_handle = r.bits(1)
        if not has_handle:
            return False
        handle = r.iris_packed_u64()
        if handle is None or handle == 0:
            return False
    return False


def _skip_net_token_stream(r: _Reader) -> bool:
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return False
        if has == 0:
            return True
        token = r.iris_packed_u32()
        if token is None or token == 0:
            return False
        if r.bits(1) is None or r.bits(3) is None or r.bits(1) is None:
            return False
        n = r.bits(16)
        if n is None or not r.skip(n * 8):
            return False
    return False


def _parse_data_stream(
    bits: Bits, start: int, bit_count: int, batches: list[IrisBatch], destroyed: list[int] | None = None
) -> tuple[bool, bool, str | None]:
    """Returns (ok, complete, incomplete_reason)."""
    end = start + bit_count
    r = _Reader(bits.big, end, start)
    count_m1, stream_mask = r.bits(5), r.bits(5)
    if count_m1 is None or stream_mask is None:
        return False, True, None
    stream_count = count_m1 + 1
    if stream_count < 1 or stream_count > 32 or stream_mask == 0 or stream_mask >> stream_count:
        return False, True, None
    if stream_mask & 2 == 0:
        return True, True, None
    if stream_mask & 1 and not _skip_net_token_stream(r):
        return True, False, "unsupported-net-token-stream"
    batch_count, destruction_count = r.bits(16), r.bits(16)
    if batch_count is None or destruction_count is None:
        return False, True, None
    if batch_count >= _MAX_BATCHES or destruction_count > batch_count:
        return False, True, None
    for _ in range(destruction_count):
        dead = r.iris_packed_u64()
        if dead is None:
            return False, True, None
        if destroyed is not None:
            destroyed.append(dead)
        has_info = r.bits(1)
        if has_info is None:
            return False, True, None
        if has_info and r.iris_packed_u64() is None:
            return False, True, None
        if not r.skip(1):
            return False, True, None
    for _ in range(destruction_count, batch_count):
        inline = r.bits(1)
        if inline is None:
            return False, True, None
        if inline:
            return True, False, "inline-destruction"
        handle = r.iris_packed_u64()
        n = r.bits(16) if handle is not None else None
        if handle is None or n is None:
            return False, True, None
        b_start = r.pos
        b_end = b_start + n
        if n < 2 or b_end > end:
            return False, True, None
        has_owner = bits.is_set(b_start)
        has_exports = bits.is_set(b_start + 1)
        batches.append(IrisBatch(handle, b_start + 2, n - 2, has_owner, has_exports))
        r.pos = b_end
        if has_exports and not _skip_exports(r):
            return True, False, "unsupported-object-exports"
    if r.pos == end:
        return True, True, None
    return True, False, "trailing-data-stream-bits"


def _read_bunch_header(r: _Reader) -> tuple[int, int] | None:
    """Returns (channel_index, payload_bit_count) or None."""
    is_control = r.bits(1)
    if is_control is None:
        return None
    is_open = is_close = 0
    if is_control:
        o, c = r.bits(1), r.bits(1)
        if o is None or c is None:
            return None
        is_open, is_close = o, c
    if is_close and r.serialized_int(15) is None:
        return None
    if not r.skip(1):
        return None
    is_reliable = r.bits(1)
    channel = r.legacy_packed_u32()
    if is_reliable is None or channel is None:
        return None
    if r.bits(1) is None or r.bits(1) is None:
        return None
    is_partial = r.bits(1)
    if is_partial is None:
        return None
    if is_reliable and r.serialized_int(1024) is None:
        return None
    if is_partial and not r.skip(3):
        return None
    if is_open or is_reliable:
        has_name = r.bits(1)
        # Mirrors the original's operator precedence exactly.
        if has_name is None or has_name != 0:
            if r.legacy_packed_u32() is None:
                return None
        elif not r.skip_fstring_and_number():
            return None
    payload_bits = r.serialized_int(8192)
    if payload_bits is None:
        return None
    return channel, payload_bits


def parse_iris_packet(payload: bytes) -> IrisPacket | None:
    """Read the UE 5.5 Iris replication envelope; exposes batch boundaries."""
    if not payload:
        return None
    bits = Bits(payload)
    packet_end = bits.last_one_before(bits.nbits)
    if packet_end < 7:
        return None
    r = _Reader(bits.big, packet_end)
    if r.serialized_int(4) is None or r.serialized_int(8) is None:
        return None
    handshake = r.bits(1)
    if handshake is None or handshake != 0 or not r.skip(8):
        return None
    bunches_end = bits.last_one_before(packet_end)
    if bunches_end <= r.pos:
        return None
    r.limit = bunches_end
    header = r.bits(32)
    if header is None:
        return None
    sequence = (header >> 18) & 0x3FFF
    ack_words = (header & 0xF) + 1
    if not r.skip(ack_words * 32):
        return None
    if r.pos < bunches_end:
        has_frame_time = r.bits(1)
        if has_frame_time is None:
            return None
        if has_frame_time:
            if r.serialized_int(1024) is None:
                return None
            has_byte = r.bits(1)
            if has_byte is None or (has_byte != 0 and not r.skip(8)):
                return None
    batches: list[IrisBatch] = []
    destroyed: list[int] = []
    complete = True
    reason: str | None = None
    has_stream = False
    while bunches_end - r.pos >= 10:
        bunch = _read_bunch_header(r)
        if bunch is None or bunch[1] > bunches_end - r.pos:
            return None
        channel, n = bunch
        start = r.pos
        if channel == 2:
            has_stream = True
            ok, stream_complete, stream_reason = _parse_data_stream(bits, start, n, batches, destroyed)
            if not ok:
                return None
            complete = complete and stream_complete
            reason = reason or stream_reason
        if not r.skip(n):
            return None
    if r.pos != bunches_end:
        return None
    return IrisPacket(sequence, has_stream, complete, batches, reason, tuple(destroyed))


# ---------------------------------------------------------------- other actors' location (TRD-050)
#
# Derived from the recorded inbound packets in tests/fixtures (see docs/trd/other-players-on-minimap.md,
# "TRD-050 Findings"). 55 batches of 28 distinct handles across 8 recorded packets share one layout:
#
#   33 bits  fixed state header, always 0x20E822A8 (LSB-first)      <- identifies this replicated state
#    7 bits  FVector_NetQuantize100 header: (bit 6 set) | component bit count (6 bits)
#  3*comp    x, y, z as signed raw = cm * 100 (same quantisation as the outbound own-player move)
#   37 bits  trailing state, NOT decoded (rotation/velocity?; unverified)
#
# so data_bit_count == 77 + 3*comp exactly (155 -> comp 26, 158 -> comp 27 in every recorded batch).
# Nothing here says what KIND of actor it is (player, AI, ...): the class is carried by object exports,
# which the envelope walk skips and no fixture contains. Name and vitals of these actors are not decoded.

ACTOR_STATE_HEADER = 0x20E822A8
ACTOR_STATE_HEADER_BITS = 33
ACTOR_STATE_TAIL_BITS = 37
# Recorded batches only show 26 and 27 bits. The size equation plus the 33-bit state header are the real
# discriminators, so narrower widths (actors near the world origin / low) are accepted; unobserved, 16 is a floor.
ACTOR_MIN_COMP_BITS = 16


@dataclass(frozen=True, slots=True)
class ActorLocation:
    net_ref_handle: int
    x: float  # Unreal cm, same axes and units as MovementCandidate
    y: float
    z: float
    component_bit_count: int


def decode_actor_location(bits: Bits, batch: IrisBatch) -> ActorLocation | None:
    """Location of the replicated actor in ``batch`` if it has the recorded location layout, else None."""
    n = batch.data_bit_count
    comp, rem = divmod(n - ACTOR_STATE_HEADER_BITS - _HEADER_BITS - ACTOR_STATE_TAIL_BITS, 3)
    if rem or comp < ACTOR_MIN_COMP_BITS or comp > _MAX_COMP_BITS:
        return None
    base = batch.data_bit_offset
    if bits.read(base, ACTOR_STATE_HEADER_BITS) != ACTOR_STATE_HEADER:
        return None
    header = bits.read(base + ACTOR_STATE_HEADER_BITS, _HEADER_BITS)
    if not header & 64 or (header & 63) != comp:
        return None
    start = base + ACTOR_STATE_HEADER_BITS + _HEADER_BITS
    rx, ry, rz = (_read_signed(bits, start + comp * i, comp) for i in range(3))
    if not _canonical(rx, ry, rz, comp):
        return None
    x, y, z = rx / 100.0, ry / 100.0, rz / 100.0
    if not _plausible_world(x, y, z):
        return None
    return ActorLocation(batch.net_ref_handle, x, y, z, comp)


def decode_actor_locations(payload: bytes, packet: IrisPacket | None = None) -> list[ActorLocation]:
    """Every actor location in one inbound replication datagram (the last one wins per handle)."""
    packet = packet or parse_iris_packet(payload)
    if packet is None:
        return []
    bits = Bits(payload)
    found: dict[int, ActorLocation] = {}
    for batch in packet.batches:
        loc = decode_actor_location(bits, batch)
        if loc is not None:
            found[loc.net_ref_handle] = loc
    return list(found.values())
