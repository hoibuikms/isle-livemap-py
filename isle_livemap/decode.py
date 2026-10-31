"""Bit-level decoders for the player's OWN game traffic and, from the same inbound stream, for other actors.

The movement and envelope walks are ported from the open-source (MIT) UnrealMovementPacketDecoder.cs and
UnrealIrisPacketParser.cs of Undo-IsleLiveMap (the destruction-record layout differs, see the envelope walk). The
export sections, creation records, identity tracker, location layouts and the other-creature vitals block are own
code written from a live capture (docs/trd/other-players-on-minimap.md, section 13). Everything here is a pure
function of captured bytes: nothing is sent, modified or injected.

Bit order is Unreal's: LSB-first inside each byte, bytes in stream order. That
is exactly the bit order of ``int.from_bytes(payload, "little")``, so a payload
is turned into one big integer and bit fields are read with shift and mask.
"""

from __future__ import annotations

import math
import re
import struct
from collections import Counter
from dataclasses import dataclass, field

from .species import asset_species, conventional_class_path, folder_species, stage_of_path

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
# The playable map in Unreal cm, from the Gateway projection (geo.X_OFFSET / MAP_WIDTH / WORLD_SCALE: x -505 000 .. 607 000,
# y -607 000 .. 509 000) plus 1 km all round. A creature's position found without an anchor must lie on it.
PLAYABLE_X_CM = (-605_000.0, 707_000.0)
PLAYABLE_Y_CM = (-707_000.0, 609_000.0)
# Largest acceleration component (cm/s²) a move record may carry. Fast movers send more than 100 000 (recorded: 105 000 to
# 110 000 on a flyer at 25 m/s): with the old cap those moves did not parse, the marker stood still for up to 15 s and then
# jumped 200-400 m. 2 000 000 gives the same decode as 400 000 on every recorded capture.
_MAX_ACCEL = 400_000.0
_MAX_CLIENT_TS = 10_000_000.0
# A client clock below a millisecond is not a clock: it is some other 32 bits that happen to sit where a timestamp would. The
# prefix reader used to stop at the first acceleration width whose header matched, and a false 4-bit match with a timestamp of
# 5.6e-17 hid the true 20-bit one (recorded session: 12 s of moves ignored, the marker stood still, then jumped).
_MIN_CLIENT_TS = 1e-3


def _read_signed(bits: Bits, offset: int, count: int) -> int:
    value = bits.read(offset, count)
    sign = 1 << (count - 1)
    return (value ^ sign) - sign


# The client's own move packets quantise a location in hundredths of a cm (FVector_NetQuantize100: raw / 100 = cm). The
# server's replicated state and creation vectors (Iris) use binary fixed point: raw / 128 = cm. Reading them with the /100
# of the move packets made every other creature 1.28 times too far from the world origin in x, y and z, so a player standing
# next to you was drawn 0.28 x your own distance from the origin away (650-850 m at 2.5-3 km from the origin), in the
# direction away from it. Recorded evidence: in five captures on three servers the creatures drawn 650-850 m away land
# 5-100 m from the own position when divided by 1.28 (rms 55 m, best fit 1.276-1.28), their heights match the own height
# to 1-3 m (ratio 1.287..1.297 before the division), and the user's own report in one of them (about 110 m in the game,
# 793 m on the map) comes out at 104 m.
STATE_VECTOR_SCALE = 100.0 / 128.0


def vector_to_cm(raw, scale: float = 1.0) -> tuple[float, float, float]:
    """Quantised vector integers -> Unreal cm: the single place for the /100 of the client's move packets. ``scale``
    converts other fixed-point units (``STATE_VECTOR_SCALE``: replicated state, creation records)."""
    return (raw[0] / 100.0 * scale, raw[1] / 100.0 * scale, raw[2] / 100.0 * scale)


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
        if not math.isfinite(ts) or ts < _MIN_CLIENT_TS or ts > _MAX_CLIENT_TS:
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
        x, y, z = vector_to_cm((rx, ry, rz))
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


# ---------------------------------------------------------------- the server's own copy of the own location
#
# From time to time (a correction, and when the pawn is re-synchronised) the server sends the owner's location as three
# IEEE doubles (cm) at any bit offset of an inbound packet, repeated in the next few packets. It is the one place where
# the own position is known in the server's frame: in all ten recorded connections (flying, walking and swimming) it
# equals the position decoded from the client's own moves to 0-6 m (29 m at 15 m/s with 2 s of latency), so the own
# marker and the replicated creatures share a frame. ``tools/`` and the session log use it to verify that.

_DOUBLE_HI = rb"[\x40\x41\xc0\xc1]"  # top byte of a double of magnitude 2 .. 2^21 (positions in cm are below 8e5)
_TRIPLE_RE = re.compile(rb"(?=(.{7}" + _DOUBLE_HI + rb".{7}" + _DOUBLE_HI + rb".{7}" + _DOUBLE_HI + rb"))", re.S)
_SERVER_LOC_MIN_CM = 1e4  # x and y of a real position are both > 100 m from the world origin; smaller triples are other data


def find_server_locations(payload: bytes) -> list[tuple[float, float, float]]:
    """Every plausible (x, y, z) in cm that the packet carries as three consecutive doubles, at any bit offset. Most
    packets have none; a few chance triples exist, so a caller trusts one only when a second packet repeats it."""
    n = len(payload)
    if n < 24:
        return []
    value = int.from_bytes(payload, "little")
    out: list[tuple[float, float, float]] = []
    for shift in range(8):
        data = ((value >> shift) & ((1 << (8 * n - shift)) - 1)).to_bytes(n, "little")
        for m in _TRIPLE_RE.finditer(data):
            x, y, z = struct.unpack_from("<ddd", data, m.start())
            if (
                math.isfinite(x) and math.isfinite(y) and math.isfinite(z)
                and _SERVER_LOC_MIN_CM < abs(x) < _MAX_WORLD_XY and _SERVER_LOC_MIN_CM < abs(y) < _MAX_WORLD_XY
                and 1.0 < abs(z) < _MAX_WORLD_Z
            ):
                out.append((x, y, z))
    return out


# ---------------------------------------------------------------- Iris envelope


@dataclass(frozen=True, slots=True)
class TokenExport:
    """One net-token string export: the sender defines ``text`` for ``(type_id, index)``.

    Observed type ids in a live capture: 1 = names (bone/socket names, player names), 2 = gameplay tags
    ("Ability.Skill.Use"). ``raw`` is the string as sent; ``text`` is the decoded form (see ``decode_net_string``)."""

    index: int
    type_id: int
    authority: int
    text: str
    raw: bytes = b""


@dataclass(frozen=True, slots=True)
class ObjectExport:
    """One level of an exported object reference chain: ``handle`` is named ``name`` (inline string, or the
    earlier-defined object-name token ``name_token``) inside ``outer_handle`` (None = root/package or unknown)."""

    handle: int
    name_token: int | None = None
    name: str | None = None
    outer_handle: int | None = None
    flag: int = 0  # the one undecoded bit between "has path" and "has name"; 0 and 1 both occur


@dataclass(frozen=True, slots=True)
class ExportSection:
    tokens: tuple[TokenExport, ...] = ()
    objects: tuple[ObjectExport, ...] = ()
    must_be_mapped: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class IrisBatch:
    net_ref_handle: int
    data_bit_offset: int
    data_bit_count: int
    has_owner_data: bool
    has_exports: bool
    exports: ExportSection | None = None  # set when ``has_exports`` and the section parsed


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


def decode_net_string(encoded: bool, raw: bytes) -> str:
    """Text of a net string. Unencoded strings (flag bit 0: asset paths, tags) are plain bytes. Encoded ones
    (flag bit 1: names that may hold non-ASCII text) carry one code point per base-128 group, most significant
    group first, every group but the last having bit 7 set: ``0x41`` = "A", ``0x81 0x60`` = U+00E0, ``0x82 0x10`` =
    U+0110, ``0x80 0xBD 0x27`` = U+1EA7. Checked against Vietnamese player names in a real capture ("Cuong Sinh Ton"
    with diacritics comes out readable). A malformed tail never raises: it becomes U+FFFD."""
    if not encoded:
        return raw.decode("utf-8", "replace")
    out: list[str] = []
    value, pending = 0, False
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
        if byte & 0x80:
            pending = True
            continue
        pending = False
        out.append(chr(value) if 0 < value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF else "\ufffd")
        value = 0
    if pending:
        out.append("\ufffd")
    return "".join(out)


def _read_net_token(r: _Reader, includes_type_id: bool) -> tuple[int, int, int] | None:
    """(index, authority bit, type id) of a net token; index 0 (= no token) gives (0, 0, 0). None = truncated."""
    token = r.iris_packed_u32()
    if token is None:
        return None
    if token == 0:
        return 0, 0, 0
    authority = r.bits(1)
    type_id = r.bits(3) if includes_type_id else 0
    if authority is None or type_id is None:
        return None
    return token, authority, type_id


def _read_net_string(r: _Reader) -> tuple[int, bytes] | None:
    """(encoded flag, bytes) of a net string: 1 flag bit, 16-bit byte count, the bytes."""
    encoded = r.bits(1)
    n = r.bits(16)
    if encoded is None or n is None or r.pos > r.limit - n * 8:
        return None
    raw = bytes(r.bits(8) for _ in range(n))
    return encoded, raw


def _read_conditional_net_string(r: _Reader) -> tuple[bool, tuple[int, bytes] | None]:
    """(ok, string | None): a presence bit, then the string when set."""
    has = r.bits(1)
    if has is None:
        return False, None
    if has == 0:
        return True, None
    got = _read_net_string(r)
    return got is not None, got


def _skip_net_ref_handle(r: _Reader) -> bool:
    has = r.bits(1)
    if has is None:
        return False
    if has == 0:
        return True
    h = r.iris_packed_u64()
    return h is not None and h != 0


def _read_object_chain(r: _Reader, out: list[ObjectExport]) -> bool:
    """One exported object reference: the object, then its outer, and so on up to the root (a level whose
    presence bit is 0). A level with no path is a reference to an object whose path was exported earlier and
    ends the chain; a level with a path names itself with a net token (string inline the first time)."""
    levels: list[tuple[int, int | None, str | None, int]] = []
    for _ in range(33):
        has_ref = r.bits(1)
        if has_ref is None:
            return False
        if has_ref == 0:
            break
        handle = r.iris_packed_u64()
        if handle is None or handle == 0:
            return False
        has_path = r.bits(1)
        if has_path is None:
            return False
        if has_path == 0:
            levels.append((handle, None, None, 0))
            break
        flag = r.bits(1)
        has_name = r.bits(1)
        if flag is None or has_name is None:
            return False
        if has_name == 0:
            levels.append((handle, None, None, flag))
            break
        token = _read_net_token(r, False)
        if token is None:
            return False
        ok, got = _read_conditional_net_string(r)
        if not ok:
            return False
        levels.append((handle, token[0] or None, decode_net_string(bool(got[0]), got[1]) if got else None, flag))
    else:
        return False  # deeper than 32 levels: not a path
    for i, (handle, token, name, flag) in enumerate(levels):
        outer = levels[i + 1][0] if i + 1 < len(levels) else None
        out.append(ObjectExport(handle, token, name, outer, flag))
    return True


def _read_inline_full_object_reference(r: _Reader, out: list[ObjectExport]) -> bool:
    exported = r.bits(1)
    if exported is None:
        return False
    if exported:  # never seen in a capture: parsed so the cursor stays aligned, contents not recorded
        return _skip_net_ref_handle(r) and _read_net_token(r, False) is not None and _read_conditional_net_string(r)[0]
    return _read_object_chain(r, out)


def _read_exports(r: _Reader) -> ExportSection | None:
    """The export section that follows a batch: (1) net-token string exports, (2) object reference exports,
    (3) the must-be-mapped handle list. None when the section does not parse (the cursor is then undefined)."""
    tokens: list[TokenExport] = []
    objects: list[ObjectExport] = []
    mapped: list[int] = []
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return None
        if has == 0:
            break
        token = _read_net_token(r, True)
        got = _read_net_string(r) if token is not None else None
        if token is None or got is None:
            return None
        tokens.append(TokenExport(token[0], token[2], token[1], decode_net_string(bool(got[0]), got[1]), got[1]))
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return None
        if has == 0:
            break
        if not _read_inline_full_object_reference(r, objects):
            return None
    for _ in range(_MAX_BATCHES):
        has = r.bits(1)
        if has is None:
            return None
        if has == 0:
            return ExportSection(tuple(tokens), tuple(objects), tuple(mapped))
        if not r.bits(1):
            return None
        handle = r.iris_packed_u64()
        if handle is None or handle == 0:
            return None
        mapped.append(handle)
    return None


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
    # Destruction record = packed u64 handle + ONE bit (always 1 in the recorded capture; meaning unknown). The
    # earlier reading (a "has info" bit, an optional second handle, a trailing bit) put every record after a
    # 0-valued bit out of step and produced garbage handles. Checked on a 193 s capture: with this layout all
    # 2849 parseable data streams end exactly on their last batch, and the batches that follow number
    # ``batch_count - destruction_count``.
    for _ in range(destruction_count):
        dead = r.iris_packed_u64()
        if dead is None or r.bits(1) is None:
            return False, True, None
        if destroyed is not None:
            destroyed.append(dead)
    for _ in range(batch_count - destruction_count):
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
        r.pos = b_end
        section = None
        if has_exports:
            section = _read_exports(r)
        batches.append(IrisBatch(handle, b_start + 2, n - 2, has_owner, has_exports, section))
        if has_exports and section is None:
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


def parse_packet_notify(payload: bytes) -> tuple[int, int, tuple[int, ...]] | None:
    """(sequence, newest acknowledged sequence, acknowledgement history words) of a datagram's packet-notify header, which
    both directions carry in front of the bunches. History bit i of the words (word 0 first, LSB first) says whether
    sequence ``acked - 1 - i`` was received. Only the header is read, never the bunches."""
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
    words = []
    for _ in range((header & 0xF) + 1):
        w = r.bits(32)
        if w is None:
            return None
        words.append(w)
    return (header >> 18) & 0x3FFF, (header >> 4) & 0x3FFF, tuple(words)


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


# ---------------------------------------------------------------- batch kind (state update vs creation)
#
# Fact base (docs/trd/reference-findings.md, 3.4 and 5): a batch payload starts with 3 bits that are zero, then
# a "has state" bit, then an "is creation" bit. The older fixtures' batches all begin 0,0,0,1,0 (LSB-first), i.e. a
# state update; the live join capture has 33 362 state, 5 419 creation (0,0,0,1,1) and 884 other-prefix batches. ``batch_kind`` classifies this prefix only; ``decode_creation`` reads the record.

KIND_STATE = "state"
KIND_CREATION = "creation"
KIND_UNKNOWN = "unknown"
_KIND_PREFIX_BITS = 5
_KIND_SKIPPED_MASK = 0b00111
_KIND_HAS_STATE = 1 << 3
_KIND_IS_CREATION = 1 << 4


def batch_kind(bits: Bits, batch: IrisBatch) -> str:
    """"state" / "creation" / "unknown" from the first five payload bits of ``batch`` (nothing else is read)."""
    if batch.data_bit_count < _KIND_PREFIX_BITS or batch.data_bit_offset < 0:
        return KIND_UNKNOWN
    prefix = bits.read(batch.data_bit_offset, _KIND_PREFIX_BITS)
    if prefix & _KIND_SKIPPED_MASK or not prefix & _KIND_HAS_STATE:
        return KIND_UNKNOWN
    return KIND_CREATION if prefix & _KIND_IS_CREATION else KIND_STATE


# ---------------------------------------------------------------- actor creation records
#
# Facts (193 s live capture, 5419 creation batches; see docs/trd/other-players-on-minimap.md, "Identity"). A
# creation batch payload is, LSB-first:
#
#    5 bits  prefix: 3 zero bits, has-state = 1, is-creation = 1
#    1 bit   has-baseline; when set, 2 more bits follow
#    3 bits  factory id (0 in every record)
#   32 bits  protocol id (a hash of the replicated state layout; the same class always repeats it)
#    1 bit   has-archetype; 0 for level-placed (static) actors, which are named by their own object export instead
#    3 bits  archetype reference flags, always 0,0,1 (client-ref, exported, valid), then a packed u64 handle: the
#            archetype's object handle, resolved to a path through the export sections (``IdentityTracker``)
#    1 bit   persistent level; 0 would be followed by a level reference (not decoded: no location is read then)
#    1 bit   has-location; then 1 valid bit and a quantised vector (7-bit header, bit 6 set | component bit count,
#            then 3 signed components). Read like the state vectors (raw / 128 cm, ``STATE_VECTOR_SCALE``) it is the world
#            position WITHOUT the factor 16: the same actor's later state vector is exactly 16 times larger in x and y
#            (raw 44992560 vs 2812035 for one fish; 15.99..16.00 for five actors). Read with the old /100 the creation
#            positions landed inside +-700 m of the world origin while the world is +-8 km wide; ``CREATION_LOCATION_SCALE``
#            (16 / 1.28 = 12.5 relative to /100) puts them in the same frame as the state vectors and the own marker.
#
# The state payload after the header is class specific and is NOT decoded here.

_CREATION_FLAGS_ARCHETYPE = 0b100  # LSB-first (0, 0, 1)
CREATION_LOCATION_SCALE = 16.0 * STATE_VECTOR_SCALE  # 12.5: the creation vector is 1/16 of the state vector (same actor, exactly)


@dataclass(frozen=True, slots=True)
class Creation:
    net_ref_handle: int
    archetype_handle: int | None  # None: level-placed actor (no archetype in the record)
    protocol_id: int
    location: tuple[float, float, float] | None  # world cm (scaled, see CREATION_LOCATION_SCALE), None if absent
    header_bits: int  # bits of the batch consumed by the fields above


def decode_creation(bits: Bits, batch: IrisBatch) -> Creation | None:
    """The creation record at the start of ``batch`` (None when it is not a creation or does not parse)."""
    if batch_kind(bits, batch) != KIND_CREATION:
        return None
    start = batch.data_bit_offset
    r = _Reader(bits.big, start + batch.data_bit_count, start + _KIND_PREFIX_BITS)
    baseline = r.bits(1)
    if baseline is None or (baseline and not r.skip(2)):
        return None
    factory, protocol, has_archetype = r.bits(3), r.bits(32), r.bits(1)
    if factory is None or protocol is None or has_archetype is None:
        return None
    archetype = None
    if has_archetype:
        flags = r.bits(3)
        if flags != _CREATION_FLAGS_ARCHETYPE:
            return Creation(batch.net_ref_handle, None, protocol, None, r.pos - start)
        archetype = r.iris_packed_u64()
        if archetype is None:
            return None
    persistent, has_location = r.bits(1), r.bits(1)
    location = None
    if persistent and has_location and r.bits(1) == 1:
        header = r.bits(_HEADER_BITS)
        comp = None if header is None else header & 63
        if header is not None and header & 64 and _MIN_COMP_BITS <= comp <= _MAX_COMP_BITS and r.pos + 3 * comp <= r.limit:
            raw = [_read_signed(bits, r.pos + comp * i, comp) for i in range(3)]
            x, y, z = vector_to_cm(raw, CREATION_LOCATION_SCALE)
            if _canonical(*raw, comp) and _plausible_world(x, y, z):
                location = (x, y, z)
                r.pos += 3 * comp
    return Creation(batch.net_ref_handle, archetype, protocol, location, r.pos - start)


# ---------------------------------------------------------------- identity tracker: handle -> class (-> name)
#
# Export sections arrive with (before, or in the same batch as) the data that uses them, but an archetype
# handle can also be exported by an earlier datagram, so the tracker keeps the tables for the whole connection
# and resolves a path lazily (``class_path``). Handles are reused by the server, so a destruction record drops
# the actor's record and a later creation of the same handle starts afresh. Two kinds of handles are
# referenced but never exported in the recorded capture (the archetype of 85 fruit actors and of 6 pteranodon
# pawns): their path stays unknown; only the asset paths that accompany their creation records are known.

_PLAYER_STATE_MARKER = "BP_PlayerState"
# A PlayerState creation record holds the player's name as a packed-u32 length + bytes (``decode_net_string``
# varint encoding) about 460..560 bits into the payload; the position moves by one byte (469 or 477 in the
# recorded capture), so a window is scanned and the first plausible text wins.
_NAME_SCAN_FROM = 460
_NAME_SCAN_TO = 600
_NAME_MAX_BYTES = 48
# A pawn's creation record references its PlayerState as a packed u64 (3-bit byte count - 1 + that many bytes). The
# 12 bits before the count vary, but the 8 bits that start 36 bits before the count are constant: 1 1 0 0 0 1 0 1
# (stream order; found before all 8 references in two captures). Several unrelated runs match the marker, so a
# candidate only counts once it equals the handle of a known PlayerState.
_PS_REF_MARKER = 0b10100011  # bits in stream order 1,1,0,0,0,1,0,1
_PS_REF_MARKER_BITS = 8
_PS_REF_MARKER_GAP = 36  # bits from the start of the marker to the byte-count field
_PS_REF_MAX_BYTES = 4
# A newer server build (recorded 2026-10-31, "jj") writes the same reference (3-bit byte count - 1, then the handle) behind a
# different marker that sits only 26 bits before the count: 0 0 0 1 1 0 0 0 (value 0b00011000). Found before 3 of 5 pawn records
# of that capture, each time as a pair of consecutive handles (X, X + 2) of which X was a known PlayerState; none of the
# 13 other pawn records of the older captures has it as a known PlayerState, and no world object or fish record has it in any
# of the four recordings. Like the first marker it only counts once a candidate equals the handle of a known PlayerState.
_PS_REF_MARKER_V2 = 0b00011000
_PS_REF_MARKER_GAP_V2 = 26
# One recorded pawn (a Giganotosaurus whose player reference is a 2-byte handle) had 139 (1,1,0,1,0,0,0,1) where every other
# of 40+ references has 163 (1,1,0,0,0,1,0,1): two of the eight bits are not constant. The variant marker (those two bits
# ignored) only counts when the strict rule found no known PlayerState, for a 2 or 3 byte handle inside the window where
# references were recorded (608..956 bits), and again only when exactly one candidate is a known PlayerState.
_PS_REF_MARKER_LOOSE_MASK = 0xD7  # ignores stream bits 3 and 5
_PS_REF_LOOSE_WINDOW = (400, 1100)
_PS_REF_LOOSE_BYTES = (2, 3)
_UNLEARNED_MAX = 4096
_HANDLE_ASSETS_MAX = 8192  # actors whose dinosaur assets are counted (a long connection creates and destroys many)
_HANDLE_ASSET_MIN = 1  # batches of one actor that must name its species folder before the species is believed (see handle_species)
_RETIRED_NAMES_MAX = 512  # PlayerState names kept after the server destroyed their PlayerState (see IdentityTracker)


def _plausible_name(text: str) -> bool:
    """Printable, Latin script (ASCII plus Latin-1 / Extended-A/B / Latin Extended Additional, where Vietnamese
    lives): rejects the random code points a mis-aligned scan decodes."""
    if not text or len(text) > 40:
        return False
    for ch in text:
        o = ord(ch)
        if not (0x20 <= o < 0x7F or 0xA0 <= o <= 0x24F or 0x1E00 <= o <= 0x1EFF) or not ch.isprintable():
            return False
    return text.strip() != ""


def decode_player_name(bits: Bits, batch: IrisBatch) -> str | None:
    """The player name inside a PlayerState creation payload, or None when no plausible text is found."""
    base, n = batch.data_bit_offset, batch.data_bit_count
    for o in range(_NAME_SCAN_FROM, min(_NAME_SCAN_TO, n - 18)):
        if bits.read(base + o, 2):
            continue
        ln = bits.read(base + o + 2, 8)
        if not 1 <= ln <= _NAME_MAX_BYTES or o + 10 + ln * 8 > n:
            continue
        raw = bits.read(base + o + 10, ln * 8).to_bytes(ln, "little")
        if raw[-1] & 0x80:
            continue
        text = decode_net_string(True, raw)
        if _plausible_name(text):
            return text
    return None


@dataclass(slots=True)
class ActorInfo:
    """What the creation record of one actor handle says (all of it from the wire; nothing guessed)."""

    handle: int
    archetype_handle: int | None
    location: tuple[float, float, float] | None
    protocol_id: int
    asset_species: tuple[str, ...] = ()  # /Characters/Dinosaurs/<folder>/ assets named in the creation batch
    player_state: int | None = None  # handle of the PlayerState the record references (pawns)
    name: str | None = None  # PlayerState records only: the player name
    ref_candidates: tuple[int, ...] = ()  # pawns: handles that look like a PlayerState reference (resolved when known)
    ref_candidates_loose: tuple[int, ...] = ()  # the same behind the variant marker (used when no strict candidate is known)
    creation_bits: int = 0  # size of the creation batch: a pawn's is thousands of bits, a burrow's a few hundred


class IdentityTracker:
    """Export tables + creation records of one connection. Feed it every inbound datagram, in order."""

    def __init__(self, class_cache=None) -> None:
        self.class_cache = class_cache  # classcache.ClassCache | None: protocol id -> class path from earlier sessions
        self.reset()

    def reset(self) -> None:
        self._unlearned: set[int] = set()  # handles whose archetype path was not exported yet (offered to the cache later)
        self.table_classes = 0  # creations whose class path this connection's export tables gave (0: tables missed)
        self._object_names: dict[int, str] = {}  # object-name token index -> text (the implicit-type token space)
        self.tokens: dict[tuple[int, int], str] = {}  # (type id, index) -> text: names, gameplay tags
        self.objects: dict[int, ObjectExport] = {}
        self.actors: dict[int, ActorInfo] = {}
        self._arch_votes: dict[int, Counter] = {}  # archetype handle -> {species folder: creations that named it}
        # Actor handle -> {species folder: batches of that actor whose export section named an asset of the folder}, from
        # EVERY batch (a pawn's animation montages are exported in its state batches, long after, or without, its creation
        # record). A handle that names exactly one folder is that species; one that names several is a relay (the own
        # pawn's batches carry the montages of every species around it) and says nothing about itself.
        self._handle_assets: dict[int, Counter] = {}
        self._handle_stage: dict[int, str] = {}  # actor handle -> newest growth stage an animation asset of it states
        self._asset_learned: set[int] = set()  # handles whose asset-derived species was offered to the class cache
        self.player_names: dict[int, str] = {}  # PlayerState handle -> name
        # Names of PlayerStates the server destroyed (a player out of relevancy, a PlayerState re-created): a pawn that is
        # created again keeps referencing the same PlayerState handle, so its name stays known for the whole connection.
        # A re-created PlayerState on the same handle replaces the entry (the server's newest word wins).
        self._retired_names: dict[int, str] = {}
        self._waiting: set[int] = set()  # pawns with reference candidates and no known PlayerState yet
        self.creations = 0
        self.exports_seen = 0  # object-export levels recorded
        self.destroyed_known = 0  # destruction records that matched a creation seen in this connection
        self.destroyed_unknown = 0

    # -- queries ------------------------------------------------------------
    def object_path(self, handle: int | None) -> str | None:
        """Full path ``/Package.Name`` of an exported object (outer chain joined with "."), None if unknown."""
        parts: list[str] = []
        seen: set[int] = set()
        while handle is not None and handle not in seen:
            seen.add(handle)
            obj = self.objects.get(handle)
            if obj is None:
                return None if not parts else ".".join(reversed(parts))
            name = obj.name if obj.name is not None else self._object_names.get(obj.name_token or 0)
            if name is None:
                return None if not parts else ".".join(reversed(parts))
            parts.append(name)
            handle = obj.outer_handle
        return ".".join(reversed(parts)) if parts else None

    def class_path(self, handle: int) -> str | None:
        """Path of the archetype of actor ``handle`` (level-placed actors: of the actor itself), None if unknown."""
        info = self.actors.get(handle)
        if info is None:
            return None
        path = self.object_path(info.archetype_handle if info.archetype_handle is not None else handle)
        if path is None and self.class_cache is not None and info.archetype_handle is not None:
            return self.class_cache.lookup(info.protocol_id)  # the join tables were missed: what an earlier session saw
        return path

    def tables_missed(self) -> bool:
        """True when almost no creation of this connection got its class path from the export tables: the capture
        began after the join (the server sends those tables once; a later class only exports its own archetype)."""
        return self.table_classes * 10 <= self.creations

    def species_source(self, handle: int) -> str | None:
        """Where the class / species of actor ``handle`` comes from: "table" (this connection's export tables, the only
        confirmed one), "cache" (saved class cache) or "seed" (shipped with the app) by protocol id, "asset" (the dinosaur
        assets named in its creation records or in any of its batches), None when nothing is known."""
        info = self.actors.get(handle)
        if info is None:
            return "asset" if self.handle_species(handle) else None  # no creation record: only its own assets can say
        if self.object_path(info.archetype_handle if info.archetype_handle is not None else handle) is not None:
            return "table"
        if info.archetype_handle is None:
            return "asset" if self.handle_species(handle) else None
        folder = self.archetype_species(info.archetype_handle) or self.handle_species(handle)
        if self.class_cache is not None:
            source = self.class_cache.source(info.protocol_id)
            if source is not None:
                if folder and folder_species(self.class_cache.lookup(info.protocol_id)) == folder:
                    return "asset"  # the remembered class is the one this connection's own assets name: confirmed
                return source
        return "asset" if folder or info.asset_species else None

    def is_inferred(self, handle: int) -> bool:
        """True when the species of ``handle`` comes from the saved class cache or the shipped seed, not from this
        connection. A species deduced from the creature's own creation-record assets ("asset") is wire evidence of this
        connection and is not marked; ``species_source`` still tells the two apart for the logs and the report."""
        return self.species_source(handle) in ("cache", "seed")

    def archetype_species(self, archetype: int | None) -> str | None:
        """Species of an archetype whose class path was never exported, from the dinosaur assets that accompany the
        creation records of that archetype: used only when at least two creations name one folder and none names
        another (in the recorded capture: archetype 2221, five creations, all Pteranodon animation montages)."""
        votes = self._arch_votes.get(archetype) if archetype is not None else None
        if not votes or len(votes) != 1:
            return None
        folder, count = next(iter(votes.items()))
        return folder if count >= 2 else None

    def species_hint(self, handle: int) -> str | None:
        """Species folder of actor ``handle`` from dinosaur assets, when its class path is not known (the path decides
        otherwise): the assets of its archetype's creation records (``archetype_species``), else the assets named in its
        own batches (``handle_species``; also for an actor whose creation record was never captured)."""
        info = self.actors.get(handle)
        if info is not None:
            if self.class_path(handle):
                return None
            folder = self.archetype_species(info.archetype_handle)
            if folder:
                return folder
        return self.handle_species(handle)

    def handle_species(self, handle: int) -> str | None:
        """Species named by the dinosaur assets (animation montages, sounds) exported in the batches of actor ``handle``:
        only when every such batch names one folder. An actor that names several folders is a relay for the montages around
        it (in the recorded captures: the own pawn, whose batches carry the montages of every species nearby) and says
        nothing about itself. In the recorded captures 14 of 14 single-folder actors whose class was known named their own
        species, and a pawn's montages arrive minutes apart, so one batch is enough (a requirement of two never fired
        before the server destroyed the creature again)."""
        counts = self._handle_assets.get(handle)
        if not counts or len(counts) != 1:
            return None
        folder, n = next(iter(counts.items()))
        return folder if n >= _HANDLE_ASSET_MIN else None

    def handle_stage(self, handle: int) -> str:
        """Growth stage (Hatchling / Juvenile / Subadult / Adult) the newest animation asset of actor ``handle`` states;
        "" when no asset of it states one, or when it is not a single-species actor (``handle_species``)."""
        return self._handle_stage.get(handle, "") if self.handle_species(handle) else ""

    # -- ingestion ----------------------------------------------------------
    def _record_exports(self, section: ExportSection) -> None:
        for t in section.tokens:
            self.tokens[(t.type_id, t.index)] = t.text
        for o in section.objects:
            if o.name_token and o.name is not None:
                self._object_names[o.name_token] = o.name
            if o.name is not None or o.name_token:
                self.objects[o.handle] = o
                self.exports_seen += 1

    def _assets_of(self, section: ExportSection | None) -> list[tuple[str, str]]:
        """(species folder, growth stage or "") of the dinosaur assets an export section defines or maps."""
        if section is None:
            return []
        found: dict[str, str] = {}
        for handle in [o.handle for o in section.objects] + list(section.must_be_mapped):
            path = self.object_path(handle)
            folder = asset_species(path)
            if folder:
                stage = stage_of_path(path)
                if folder not in found or (stage and not found[folder]):
                    found[folder] = stage
        return list(found.items())

    def _folders_of(self, section: ExportSection | None) -> tuple[str, ...]:
        """Dinosaur species folders named by the objects an export section defines or maps (must-be-mapped)."""
        return tuple(f for f, _ in self._assets_of(section))

    def _count_assets(self, packet: IrisPacket) -> None:
        """Every batch whose export section names dinosaur assets counts them for its actor handle (once per folder)."""
        for b in packet.batches:
            if b.exports is None:
                continue
            for folder, stage in self._assets_of(b.exports):
                counts = self._handle_assets.get(b.net_ref_handle)
                if counts is None:
                    if len(self._handle_assets) >= _HANDLE_ASSETS_MAX:
                        for old in list(self._handle_assets)[: _HANDLE_ASSETS_MAX // 4]:  # oldest first (dict order)
                            self._handle_assets.pop(old, None)
                            self._handle_stage.pop(old, None)
                    counts = self._handle_assets[b.net_ref_handle] = Counter()
                counts[folder] += 1
                if stage:
                    self._handle_stage[b.net_ref_handle] = stage
            self._learn_asset_class(b.net_ref_handle)

    OBJECT_RECORD_BITS = 1000  # a pawn's creation batch is >= 2000 bits in every recording; a burrow's is 230-270

    def is_object_record(self, handle: int) -> bool:
        """True for an actor whose class path was never exported but whose small creation record names dinosaur assets: a
        burrow or nest that sits under a dinosaur's folder (the join tables were missed), not a creature."""
        info = self.actors.get(handle)
        bits = getattr(info, "creation_bits", 0)
        return (info is not None and 0 < bits < self.OBJECT_RECORD_BITS and bool(info.asset_species)
                and not self.class_path(handle))

    def _learn_asset_class(self, handle: int) -> None:
        """Offer the class cache the species an actor's own assets name, once, for an actor whose class path no table gave:
        a later session that misses the join tables then names that protocol id (marked "~" there)."""
        if self.class_cache is None or handle in self._asset_learned:
            return
        info = self.actors.get(handle)
        folder = self.handle_species(handle)
        if info is None or info.archetype_handle is None or not folder or self.is_object_record(handle):
            return
        if self.object_path(info.archetype_handle) is not None or self.class_cache.source(info.protocol_id) is not None:
            return  # a table or an earlier entry already says it
        self._asset_learned.add(handle)
        self.class_cache.learn(info.protocol_id, conventional_class_path(folder))

    def _reference_candidates(self, bits: Bits, batch: IrisBatch, start: int) -> tuple[int, ...]:
        """Packed handles that follow the PlayerState-reference marker in ``batch`` (canonical width only)."""
        base, n = batch.data_bit_offset, batch.data_bit_count
        found: list[int] = []
        for o in range(max(start, _PS_REF_MARKER_GAP), n - 11):
            if bits.read(base + o - _PS_REF_MARKER_GAP, _PS_REF_MARKER_BITS) != _PS_REF_MARKER:
                continue
            size = bits.read(base + o, 3)
            if size >= _PS_REF_MAX_BYTES or o + 3 + (size + 1) * 8 > n:
                continue
            handle = bits.read(base + o + 3, (size + 1) * 8)
            if handle >> (size * 8) and handle not in found:  # top byte non-zero: the shortest encoding
                found.append(handle)
        if n < self.OBJECT_RECORD_BITS:
            return tuple(found)  # a small record is an object an actor owns (burrow, marker), not a pawn: no creature to name
        for o in range(max(start, _PS_REF_MARKER_GAP_V2), n - 11):  # the newer build's marker (see _PS_REF_MARKER_V2)
            if bits.read(base + o - _PS_REF_MARKER_GAP_V2, _PS_REF_MARKER_BITS) != _PS_REF_MARKER_V2:
                continue
            size = bits.read(base + o, 3)
            if size >= _PS_REF_MAX_BYTES or o + 3 + (size + 1) * 8 > n:
                continue
            handle = bits.read(base + o + 3, (size + 1) * 8)
            if handle >> (size * 8) and handle not in found:
                found.append(handle)
        return tuple(found)

    def _loose_reference_candidates(self, bits: Bits, batch: IrisBatch, strict: tuple[int, ...]) -> tuple[int, ...]:
        """Packed 2 / 3 byte handles behind the variant marker (see ``_PS_REF_MARKER_LOOSE_MASK``), not already strict candidates."""
        base, n = batch.data_bit_offset, batch.data_bit_count
        found: list[int] = []
        target = _PS_REF_MARKER & _PS_REF_MARKER_LOOSE_MASK
        lo, hi = _PS_REF_LOOSE_WINDOW
        for o in range(max(lo, _PS_REF_MARKER_GAP), min(hi, n - 11)):
            marker = bits.read(base + o - _PS_REF_MARKER_GAP, _PS_REF_MARKER_BITS)
            if marker == _PS_REF_MARKER or marker & _PS_REF_MARKER_LOOSE_MASK != target:
                continue
            size = bits.read(base + o, 3)
            if size + 1 not in _PS_REF_LOOSE_BYTES or o + 3 + (size + 1) * 8 > n:
                continue
            handle = bits.read(base + o + 3, (size + 1) * 8)
            if handle >> (size * 8) and handle not in found and handle not in strict:
                found.append(handle)
        return tuple(found)

    def _resolve_player_state(self, info: ActorInfo) -> None:
        """Link ``info`` to the one known PlayerState among its reference candidates (none or several: no link). The candidates
        behind the variant marker are only looked at when no strict candidate is a known PlayerState."""
        def known_of(candidates):
            return {h for h in candidates if h in self.player_names or h in self._retired_names}
        known = known_of(info.ref_candidates)
        if not known:
            known = known_of(info.ref_candidates_loose)
        if len(known) == 1:
            info.player_state = next(iter(known))
            self._waiting.discard(info.handle)

    def _settle_class(self, handle: int) -> bool:
        """True once nothing is pending for ``handle``: its archetype path is known from this connection's tables
        (counted, and offered to the class cache) or it has no archetype."""
        info = self.actors.get(handle)
        if info is None or info.archetype_handle is None:
            return True
        path = self.object_path(info.archetype_handle)
        if path is None:
            return False
        self.table_classes += 1
        if self.class_cache is not None:
            self.class_cache.learn(info.protocol_id, path)
        return True

    def feed(self, payload: bytes, packet: IrisPacket | None = None) -> list[Creation]:
        """Update from one datagram; returns the creation records it carried."""
        packet = packet or parse_iris_packet(payload)
        if packet is None:
            return []
        exported = False
        for b in packet.batches:
            if b.exports is not None:
                self._record_exports(b.exports)
                exported = True
        if exported and self._unlearned:  # tables that arrive after their creations
            self._unlearned = {h for h in self._unlearned if not self._settle_class(h)}
        if exported:
            self._count_assets(packet)
        for handle in packet.destroyed_handles:
            self._waiting.discard(handle)
            self._handle_assets.pop(handle, None)
            self._handle_stage.pop(handle, None)
            self._asset_learned.discard(handle)
            if self.actors.pop(handle, None) is not None:
                self.destroyed_known += 1
            else:
                self.destroyed_unknown += 1
            gone_name = self.player_names.pop(handle, None)
            if gone_name:
                if len(self._retired_names) >= _RETIRED_NAMES_MAX:
                    self._retired_names.clear()
                self._retired_names[handle] = gone_name
        bits = Bits(payload)
        out: list[Creation] = []
        for b in packet.batches:
            creation = decode_creation(bits, b)
            if creation is None:
                continue
            self.creations += 1
            info = ActorInfo(creation.net_ref_handle, creation.archetype_handle, creation.location, creation.protocol_id,
                             self._folders_of(b.exports), creation_bits=b.data_bit_count)
            self.actors[info.handle] = info  # a reused handle replaces the old record
            self._asset_learned.discard(info.handle)
            self._learn_asset_class(info.handle)  # its own batches may already have named the species
            if info.archetype_handle is not None:
                votes = self._arch_votes.setdefault(info.archetype_handle, Counter())
                votes.update(info.asset_species)
            if not self._settle_class(info.handle) and len(self._unlearned) < _UNLEARNED_MAX:
                self._unlearned.add(info.handle)
            path = self.class_path(info.handle) or ""
            if _PLAYER_STATE_MARKER in path:
                info.name = decode_player_name(bits, b)
                if info.name:
                    self.player_names[info.handle] = info.name
                    self._retired_names.pop(info.handle, None)
                    for waiting in [self.actors[h] for h in self._waiting if h in self.actors]:
                        self._resolve_player_state(waiting)
            elif folder_species(path) or info.asset_species or not path:
                info.ref_candidates = self._reference_candidates(bits, b, creation.header_bits)
                info.ref_candidates_loose = self._loose_reference_candidates(bits, b, info.ref_candidates)
                if info.ref_candidates or info.ref_candidates_loose:
                    self._waiting.add(info.handle)
                    self._resolve_player_state(info)
            out.append(creation)
        return out

    def name_of(self, handle: int) -> str | None:
        """Player name of actor ``handle`` through the PlayerState its creation record references."""
        info = self.actors.get(handle)
        if info is None:
            return None
        if info.player_state is None:
            return None
        return self.player_names.get(info.player_state) or self._retired_names.get(info.player_state)


@dataclass(frozen=True, slots=True)
class PacketCounts:
    """Diagnostics for one datagram: how many batches of each kind, how many carry an export section and how
    many destruction records the envelope holds."""

    state: int = 0
    creation: int = 0
    unknown: int = 0
    exports: int = 0
    destroyed: int = 0


def count_batches(payload: bytes, packet: IrisPacket | None = None) -> PacketCounts:
    packet = packet or parse_iris_packet(payload)
    if packet is None:
        return PacketCounts()
    bits = Bits(payload)
    kinds = [batch_kind(bits, b) for b in packet.batches]
    return PacketCounts(
        kinds.count(KIND_STATE), kinds.count(KIND_CREATION), kinds.count(KIND_UNKNOWN),
        sum(1 for b in packet.batches if b.has_exports), len(packet.destroyed_handles))


# ---------------------------------------------------------------- other actors' location
#
# Two kinds of evidence, kept apart.
#
# 1. Fixed layouts (``ACTOR_LAYOUTS``). A state batch whose first 33 bits equal ``header`` carries a quantised
#    vector at bit ``offset`` and exactly ``tail_bits`` more bits after it, so
#
#        data_bit_count == offset + 7 + 3 * comp + tail_bits
#
#    (7 = vector header: bit 6 set | comp = bits per component; 3 signed components, raw = cm * 100, the SAME
#    frame and scale as the own outbound position). The header + the size equation + a canonical component width
#    + a plausible world position make an accidental match practically impossible. Verified layouts:
#
#      0x20E822A8 @33, tail 37   155/158-bit batches in the old fixtures AND in the live capture, where the one actor
#                                 using it (handle 61004, BP_Longear, an AI fish) updates 193 times: x/y constant,
#                                 z bobbing +-1 m, and 16 x its creation position exactly.
#      0x1680A4328 @69, tail 38  192-bit batches of four BP_Fish_RainbowFish actors (351 batches), each 15.99..16.00 x
#                                 its creation position in x and y, continuous over 130 s.
#
# 2. Learned layouts (``ActorLocationTracker``). Pawns (pteranodon in the capture) have a state layout that depends on
#    which members changed: the vector sits at a different bit offset for the same header (e.g. 0xC0000348 at 61, 91
#    and 94). No size equation exists, so a vector is only accepted when it is close to the actor's previous
#    position (or creation position) given the time passed, and an (header, offset) pair is only trusted after
#    ``TRUST_VOTES`` such confirmations. That is plausibility AND temporal continuity of the same handle.

ACTOR_STATE_HEADER = 0x20E822A8
ACTOR_STATE_HEADER_BITS = 33
ACTOR_STATE_TAIL_BITS = 37
# Recorded batches only show 26 and 27 bits. The size equation plus the 33-bit state header are the real
# discriminators, so narrower widths (actors near the world origin / low) are accepted; unobserved, 16 is a floor.
ACTOR_MIN_COMP_BITS = 16


@dataclass(frozen=True, slots=True)
class LocationLayout:
    header: int  # the first ACTOR_STATE_HEADER_BITS bits of the batch payload
    offset: int  # bit offset of the vector header inside the payload
    tail_bits: int  # bits after the vector
    note: str = ""
    z_reliable: bool = True  # False: x and y are verified, z is not (see ACTOR_LAYOUTS)


ACTOR_LAYOUTS: tuple[LocationLayout, ...] = (
    LocationLayout(ACTOR_STATE_HEADER, ACTOR_STATE_HEADER_BITS, ACTOR_STATE_TAIL_BITS, "BP_Longear (AI fish)"),
    # z of this one runs away downwards at a constant ~8 m/s for as long as the fish is replicated (25 629 -> -181 639 cm
    # in 100 s) while x and y stay put and equal 16 x the creation position: x and y are positions, z is not trusted.
    LocationLayout(0x1680A4328, 69, 38, "BP_Fish_RainbowFish (AI fish)", z_reliable=False),
)


@dataclass(frozen=True, slots=True)
class ActorLocation:
    net_ref_handle: int
    x: float  # Unreal cm, same axes and units as MovementCandidate
    y: float
    z: float
    component_bit_count: int
    z_reliable: bool = True
    yaw_deg: float | None = None  # Unreal yaw (east = 0, clockwise seen from above) that follows the vector in the same batch

    @property
    def map_heading(self) -> float | None:
        """The way the creature faces on the map: degrees clockwise from north (``yaw + 90``), None when the batch has no yaw."""
        return None if self.yaw_deg is None else map_heading_from_unreal_yaw(self.yaw_deg)


# A batch that carries a creature's position also carries its rotation: one byte, 3 bits after the end of the vector
# (the header of the vector is 7 bits, then 3 components of ``comp`` bits). Found by correlating every bit field around
# the vector with the direction of travel of 1 300+ moving creature fixes in 7 recordings: the byte x 360/256 + 90 is the
# bearing of travel to a median 2 degrees, 90 % within 21 degrees (the resolution is 1.4 degrees; wider fields add no
# precision). Not found in the batches that carry no position, so a creature that only turns on the spot is not seen.
YAW_GAP_BITS = 3
YAW_BITS = 8


def yaw_after_vector(bits: Bits, base: int, n: int, offset: int, comp: int) -> float | None:
    """Unreal yaw in degrees of the rotation byte after the vector at ``offset`` (batch of ``n`` bits starting at ``base``)."""
    pos = offset + _HEADER_BITS + 3 * comp + YAW_GAP_BITS
    if pos + YAW_BITS > n:
        return None
    return bits.read(base + pos, YAW_BITS) * 360.0 / (1 << YAW_BITS)


def _vector_at(bits: Bits, base: int, n: int, offset: int, min_comp: int,
               check_z: bool = True) -> tuple[int, float, float, float] | None:
    """(comp, x, y, z) of a canonical, plausible quantised vector at ``offset`` inside a batch of ``n`` bits."""
    if offset < 0 or offset + _HEADER_BITS > n:
        return None
    header = bits.read(base + offset, _HEADER_BITS)
    comp = header & 63
    if not header & 64 or comp < min_comp or comp > _MAX_COMP_BITS or offset + _HEADER_BITS + 3 * comp > n:
        return None
    start = base + offset + _HEADER_BITS
    raw = [_read_signed(bits, start + comp * i, comp) for i in range(3)]
    if not _canonical(raw[0], raw[1], raw[2], comp):
        return None
    x, y, z = vector_to_cm(raw, STATE_VECTOR_SCALE)
    return (comp, x, y, z) if _plausible_world(x, y, z if check_z else 0.0) else None


def decode_actor_location(bits: Bits, batch: IrisBatch) -> ActorLocation | None:
    """Location of the replicated actor in ``batch`` if it has a verified fixed layout, else None."""
    n, base = batch.data_bit_count, batch.data_bit_offset
    if n < ACTOR_STATE_HEADER_BITS:
        return None
    header = bits.read(base, ACTOR_STATE_HEADER_BITS)
    for layout in ACTOR_LAYOUTS:
        if header != layout.header:
            continue
        comp, rem = divmod(n - layout.offset - _HEADER_BITS - layout.tail_bits, 3)
        if rem or comp < ACTOR_MIN_COMP_BITS or comp > _MAX_COMP_BITS:
            continue
        vec = _vector_at(bits, base, n, layout.offset, comp, check_z=layout.z_reliable)
        if vec is not None and vec[0] == comp:
            return ActorLocation(batch.net_ref_handle, vec[1], vec[2], vec[3], comp, layout.z_reliable)
    return None


def decode_actor_locations(payload: bytes, packet: IrisPacket | None = None) -> list[ActorLocation]:
    """Every fixed-layout actor location in one inbound replication datagram (the last one wins per handle)."""
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


class ActorLocationTracker:
    """Positions of replicated actors: fixed layouts always, learned layouts when anchored (see section header).

    ``note_creation`` gives an actor its anchor (creation position, world cm). ``update(bits, batch, t, learn=True)``
    returns the actor's position for a state batch, or None. Times are datagram times in seconds."""

    MAX_SPEED = 10_000.0  # cm/s, 100 m/s: faster than any creature
    SLACK = 2_000.0  # cm: quantisation, lag and the actor moving between the two packets
    MAX_GAP = 3.0  # s: a silent actor is assumed to have moved at most this long
    ANCHOR_LIFETIME = 120.0  # s: an anchor older than this is not trusted
    TRUST_VOTES = 2
    # A vector this close to the last position needs no second sighting: by chance a 3 x 22 bit vector falls within
    # 15 m of a given point about once in 10^7 offsets tried. Walking dinosaurs do 10-17 m/s, so the tight radius
    # grows at 20 m/s (not the 100 m/s of ``MAX_SPEED``) up to ``MAX_GAP`` seconds.
    TIGHT_SLACK = 1_500.0
    TIGHT_SPEED = 2_000.0
    MIN_COMP = 22  # learned vectors: 22 bits = |raw| up to 21 km: world positions, not velocities
    SCAN_FROM, SCAN_TAIL = 5, 60
    # A creature whose creation record was missed has no anchor, so a position can only be told from the other
    # vectors of its state by where it is and how it moves. Nothing here depends on the local player: the position of
    # another creature is read from its own packets, in world coordinates. What tells a position from the other
    # vectors of a state batch:
    #   * it lies on the playable map (``PLAYABLE_*``);
    #   * it sits at an offset another creature's position was learned at for the same state header (``_trusted``): a
    #     handful of offsets instead of every one;
    #   * it continues the previous batch's candidate at creature speed, several samples in a row (``BOOT_SAMPLES``),
    #     or the same offset of the same header continues twice after a silence.
    # Chance matches fall with the number of candidates a batch offers (every offset of an unknown header: more than a
    # dozen is noise, and then only the tight radius and three samples count).
    MAX_BOOT = 5_000
    # A creature that rested sends its next state many seconds later: the same vector offset of the same state header must
    # continue (within walking speed) twice in a row before the position is believed.
    BOOT_LONG_GAP = 45.0  # s
    BOOT_LONG_SPEED = 1_600.0  # cm/s, 16 m/s: a running dinosaur
    BOOT_LONG_HITS = 2
    BOOT_SPARSE = 2  # candidates per batch at most for the offset-free rule
    # The short-gap rule accepts any vector within 20 m + 100 m/s of the previous batch's candidates: loose enough that one
    # coincidence among the many offsets of many creatures is not impossible, so the first dot of a creature with no anchor
    # needs this many consistent samples in a row. The long-silence rules are strict already (the same offset twice, or one
    # or two candidates per batch continuing at walking speed over seconds) and keep their own count.
    BOOT_SAMPLES = 3
    MAX_UNTRUSTED_CANDS = 12  # a batch of an unknown header offering more than this many map positions is noise
    # The world origin (0, 0) is where a not yet spawned or just teleported pawn is reported and where unrelated state
    # (offsets, defaults) decodes to. It is the middle of the island, so a creature can really be there; but such a vector
    # is only believed after ``BOOT_ORIGIN_HITS`` consistent samples in a row at walking pace (or, at the same offset after a
    # silence, that many moving samples: a constant decodes to the same point every time and never moves). Recorded: unrelated state that decoded to positions within
    # 450-1 600 m of the origin, at any height, wandering a few hundred metres at 50-100 m/s.
    ORIGIN_EXCLUDE_CM = 200_000.0  # 2 km around the world origin, any height: tight radius and ``BOOT_ORIGIN_HITS`` samples
    BOOT_ORIGIN_HITS = 6
    # A vector at an offset no layout is known for must have moved at least this much (in x and y) since the previous sample,
    # and in both axes: the same x and y again is a constant that decodes to a point (recorded: one point repeated by four
    # creatures, another for 77 fixes), a point whose x never changes while y does is a counter, and a creature at rest sends
    # nothing and has its spawn point on the map.
    BOOT_MOVE_CM = 50.0
    # No creature outruns this; the loose short-gap match (``SLACK`` + speed) uses it, not the 100 m/s of ``MAX_SPEED``.
    BOOT_LOOSE_SPEED = 4_500.0  # cm/s, 45 m/s

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._last: dict[int, tuple[float, float, float, float]] = {}  # handle -> (t, x, y, z)
        self._votes: Counter = Counter()
        self._trusted: dict[int, set[int]] = {}  # header -> trusted vector offsets
        self._boot: dict[int, tuple[float, list, int, dict]] = {}  # handle -> (t, [(offset, vector)], state header, hits per offset)
        self._boot_run: dict[int, int] = {}  # handle -> consecutive consistent samples of the short / sparse rules
        self.fixed = self.learned = self.rejected = self.bootstrapped = 0
        self.last_rule = ""  # which bootstrap rule placed the newest actor (diagnostics)

    @property
    def trusted_layouts(self) -> int:
        return sum(len(v) for v in self._trusted.values())

    MAX_ANCHORS = 20_000

    def note_creation(self, handle: int, location: tuple[float, float, float] | None, t: float) -> None:
        if location is None:
            self._last.pop(handle, None)
            return
        if len(self._last) >= self.MAX_ANCHORS:  # plants are created by the thousand: keep only anchors that can still be used
            self._last = {h: v for h, v in self._last.items() if t - v[0] <= self.ANCHOR_LIFETIME}
        self._last[handle] = (t, *location)

    def forget(self, handle: int) -> None:
        self._last.pop(handle, None)
        self._boot.pop(handle, None)
        self._boot_run.pop(handle, None)

    def reject_bootstrap(self, handle: int) -> None:
        """Take back the position ``bootstrap`` just gave ``handle``: it is not an anchor (the actor stays without one and is
        searched for again from the next batch)."""
        self._last.pop(handle, None)
        self._boot.pop(handle, None)
        self._boot_run.pop(handle, None)
        self.bootstrapped = max(self.bootstrapped - 1, 0)

    def _moved_like_a_creature(self, a, b) -> bool:
        """The step from vector ``b`` to ``a`` (comp, x, y, z) is a creature's: at least ``BOOT_MOVE_CM`` in x and y, and in both
        axes (a counter that ticks decodes to a point whose x or y never changes by a single centimetre)."""
        dx, dy = abs(a[1] - b[1]), abs(a[2] - b[2])
        return math.hypot(dx, dy) >= self.BOOT_MOVE_CM and dx >= 1.0 and dy >= 1.0

    def _in_origin_zone(self, vec) -> bool:
        """``vec`` (comp, x, y, z) is in the world-origin dead zone (see ``ORIGIN_EXCLUDE_CM``)."""
        return math.hypot(vec[1], vec[2]) <= self.ORIGIN_EXCLUDE_CM

    def bootstrap(self, bits: Bits, batch: IrisBatch, t: float) -> ActorLocation | None:
        """Position of an actor without a usable anchor (none, or older than ``ANCHOR_LIFETIME``): a map position in its
        state batch that continues one of the previous batch's candidates at creature speed. The player's own position
        plays no part. Two consecutive batches within ``MAX_GAP`` s are enough when the header is one whose position offsets
        are known (tight match) and three otherwise; a longer silence (a creature that rested) needs the same vector offset
        of the same state header to continue twice in a row. The accepted position anchors the actor."""
        handle, n = batch.net_ref_handle, batch.data_bit_count
        if n < self.SCAN_FROM + self.SCAN_TAIL:
            return None
        last = self._last.get(handle)
        if last is not None and 0.0 <= t - last[0] <= self.ANCHOR_LIFETIME:
            return None  # anchored: ``update`` follows it
        base = batch.data_bit_offset
        header = bits.read(base, ACTOR_STATE_HEADER_BITS)
        everything = self._map_vectors(bits, base, n, range(self.SCAN_FROM, n - self.SCAN_TAIL))
        trusted = self._trusted.get(header, ())  # the offsets another creature's position was learned at for this very header
        known = {off for off, _ in everything if off in trusted}
        # A header whose position offsets are known offers those; any other offset only while the batch offers few map
        # positions (an unknown header with more than ``MAX_UNTRUSTED_CANDS`` of them is noise).
        cands = [c for c in everything if c[0] in known or len(everything) <= self.MAX_UNTRUSTED_CANDS]
        prev = self._boot.get(handle)
        if len(self._boot) >= self.MAX_BOOT:
            self._boot.clear()
        if not cands:
            return None
        moving = [c for c in cands if not self._in_origin_zone(c[1])]  # the origin zone has its own, stricter rule below
        hits: dict[int, int] = {}
        found = None
        chained = False  # found by the loose short-gap rule: it needs BOOT_SAMPLES consistent samples in a row
        samples = self.BOOT_SAMPLES
        if prev is not None and 0.0 <= t - prev[0] <= self.BOOT_LONG_GAP:
            dt = t - prev[0]
            if dt <= self.MAX_GAP:
                # a candidate at an offset known for this header may match loosely (up to 20 m + 100 m/s); any other only at
                # walking pace, and then on three samples
                loose = self.SLACK + self.BOOT_LOOSE_SPEED * dt
                tight_r = self.TIGHT_SLACK + self.TIGHT_SPEED * dt
                pairs = []
                for c in cands:
                    for q in prev[1]:
                        if c is q:
                            continue
                        zone = self._in_origin_zone(c[1]) or self._in_origin_zone(q[1])  # tight radius and many samples
                        trusted_c = c[0] in known and not zone
                        d = math.dist(c[1][1:], q[1][1:])
                        dxy = math.dist(c[1][1:3], q[1][1:3])
                        if not trusted_c and not self._moved_like_a_creature(c[1], q[1]):
                            continue  # a constant, or a counter, at an offset no layout is known for: not a creature
                        if d <= (loose if trusted_c else tight_r):
                            pairs.append((d, c[1], dxy, trusted_c, zone))
                found = min(pairs, key=lambda r: r[0], default=None)
                # a match inside the tight radius (walking pace: chance about 10^-7 per offset) stands on two samples if the
                # offset is known for the header; a loose one (up to 20 m + 100 m/s), or any at another offset, needs a third
                chained = found is not None and (not found[3] or found[0] > tight_r)
                if found is not None:
                    self.last_rule = "chain"
                    if found[4]:
                        samples = self.BOOT_ORIGIN_HITS  # near the world origin: six samples in a row at walking pace
            if found is None and prev[2] == header:
                tight = self.TIGHT_SLACK + self.BOOT_LONG_SPEED * dt
                before = {off: vec for off, vec in prev[1]}
                for off, vec in cands:
                    q = before.get(off)
                    if q is None or math.dist(vec[1:], q[1:]) > tight:
                        continue
                    if (off not in known or self._in_origin_zone(vec)) and not self._moved_like_a_creature(vec, q):
                        continue  # a constant or a counter at an offset no layout is known for, or near the origin: not a creature
                    need = self.BOOT_ORIGIN_HITS if self._in_origin_zone(vec) else self.BOOT_LONG_HITS
                    hits[off] = prev[3].get(off, 0) + 1
                    if hits[off] >= need and (found is None or hits[off] > found[0]):
                        found = (hits[off], vec)
                        self.last_rule = "same offset"
            if found is None and dt > self.MAX_GAP and len(moving) <= self.BOOT_SPARSE and len(prev[1]) <= self.BOOT_SPARSE:
                # The fields before the position change size from batch to batch, so its offset moves; but a batch with
                # one or two map positions is nearly always a creature's position, and two such batches that continue each
                # other at walking speed are the same creature.
                tight = self.TIGHT_SLACK + self.BOOT_LONG_SPEED * dt
                pairs = [(math.dist(c[1][1:], q[1][1:]), c[1]) for c in moving for q in prev[1]
                         if not self._in_origin_zone(q[1]) and math.dist(c[1][1:], q[1][1:]) <= tight]
                if pairs:
                    found = min(pairs, key=lambda r: r[0])  # strict already (walking speed over seconds, one or two candidates)
                    self.last_rule = "sparse"
        if found is None:
            self._boot_run.pop(handle, None)  # the chain is broken: start counting again
            self._boot[handle] = (t, cands, header, hits)
            return None
        vec = found[1]
        if chained:
            run = self._boot_run.get(handle, 0) + 1
            if run < samples - 1:
                # Two samples agree; the third must continue this very vector, so the next round only compares with it.
                off = next((o for o, v in cands if v is vec), self.SCAN_FROM)
                self._boot_run[handle] = run
                self._boot[handle] = (t, [(off, vec)], header, {})
                return None
        self._boot_run.pop(handle, None)
        self._boot.pop(handle, None)
        self._last[handle] = (t, vec[1], vec[2], vec[3])
        self.bootstrapped += 1
        off = next((o for o, v in cands if v is vec), None)
        yaw = None if off is None else yaw_after_vector(bits, base, n, off, vec[0])
        return ActorLocation(handle, vec[1], vec[2], vec[3], vec[0], yaw_deg=yaw)

    def _map_vectors(self, bits: Bits, base: int, n: int, offsets) -> list:
        """(offset, (comp, x, y, z)) of every canonical vector at ``offsets`` that lies on the playable map."""
        out = []
        for off in offsets:
            vec = _vector_at(bits, base, n, off, self.MIN_COMP)
            if vec is not None and PLAYABLE_X_CM[0] <= vec[1] <= PLAYABLE_X_CM[1] and PLAYABLE_Y_CM[0] <= vec[2] <= PLAYABLE_Y_CM[1]:
                out.append((off, vec))
        return out

    def last_known(self, handle: int) -> tuple[float, float, float] | None:
        last = self._last.get(handle)
        return last[1:] if last else None

    def _radius(self, dt: float) -> float:
        return self.SLACK + self.MAX_SPEED * min(max(dt, 0.0), self.MAX_GAP)

    def update(self, bits: Bits, batch: IrisBatch, t: float, learn: bool = False) -> ActorLocation | None:
        handle = batch.net_ref_handle
        loc = decode_actor_location(bits, batch)
        if loc is not None:
            self._last[handle] = (t, loc.x, loc.y, loc.z)
            self.fixed += 1
            return loc
        if not learn:
            return None
        last = self._last.get(handle)
        n = batch.data_bit_count
        if last is None or not 0.0 <= t - last[0] <= self.ANCHOR_LIFETIME or n < self.SCAN_FROM + self.SCAN_TAIL:
            return None
        if batch_kind(bits, batch) != KIND_STATE:
            return None
        base, radius = batch.data_bit_offset, self._radius(t - last[0])
        header = bits.read(base, ACTOR_STATE_HEADER_BITS)

        def nearest(offsets) -> tuple[float, int, tuple[int, float, float, float]] | None:
            best = None
            for off in offsets:
                vec = _vector_at(bits, base, n, off, self.MIN_COMP)
                if vec is None:
                    continue
                d = math.dist(vec[1:], last[1:])
                if d <= radius and (best is None or d < best[0]):
                    best = (d, off, vec)
            return best

        best = nearest(sorted(self._trusted.get(header, ())))
        if best is None:
            best = nearest(range(self.SCAN_FROM, n - self.SCAN_TAIL))
            if best is None:
                return None
            key = (header << 16) | best[1]
            self._votes[key] += 1
            tight = self.TIGHT_SLACK + self.TIGHT_SPEED * min(max(t - last[0], 0.0), self.MAX_GAP)
            if self._votes[key] < self.TRUST_VOTES:
                if best[0] > tight:
                    return None  # a lone coincidence is not a layout
            else:
                self._trusted.setdefault(header, set()).add(best[1])
        _, off, vec = best
        self._last[handle] = (t, vec[1], vec[2], vec[3])
        self.learned += 1
        return ActorLocation(handle, vec[1], vec[2], vec[3], vec[0], yaw_deg=yaw_after_vector(bits, base, n, off, vec[0]))


# ---------------------------------------------------------------- vitals of OTHER creatures
#
# The own decoder reads GAS attribute slots (float, set flag bit, repeated float; 66 bits) by position. Creature
# pawns carry the same slots, and at least one 9-slot block is always laid out the same way:
#
#     health, maxHealth, stamina, maxStamina, maxHealth, maxHealth/2, maxHealth/2, maxHealth, maxHealth
#
# (the order of the own full frame, whose slot names the app already uses). The last five slots repeat maxHealth,
# which makes an accidental match impossible, and the values are continuous over time: 74400 health/max
# 0.784, 0.792, 0.799, 0.806, ... 0.858 over 160 s while maxHealth grows 14.7 -> 20.7; stamina varies freely.
# Growth is NOT sent with this block (its slot, one slot earlier, is only present when it changed).

_VITALS_BLOCK_SLOTS = 9
_VITALS_BLOCK_BITS = _VITALS_BLOCK_SLOTS * 66 - 1


@dataclass(frozen=True, slots=True)
class ActorVitals:
    net_ref_handle: int
    health: float
    max_health: float
    stamina: float
    max_stamina: float

    @property
    def health_pct(self) -> float:
        return min(100.0, 100.0 * self.health / self.max_health)

    @property
    def stamina_pct(self) -> float:
        return min(100.0, 100.0 * self.stamina / self.max_stamina)


def _slot(bits: Bits, absolute: int) -> float | None:
    """A GAS attribute slot at an absolute bit: float, flag bit 1, the same float again; None otherwise."""
    if absolute < 0 or absolute + 65 > bits.nbits or not bits.is_set(absolute + 32):
        return None
    raw = bits.read(absolute, 32)
    if raw != bits.read(absolute + 33, 32):
        return None
    value = f32(raw)
    return value if math.isfinite(value) and 0.0 <= value <= 1_000_000.0 else None


_MIN_VITAL_MAX = 0.5  # recorded maxima: maxHealth 2.99..1231, maxStamina 120..640; below 0.5 is a denormal coincidence


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6, abs(b) * 1e-4)


def decode_vitals_block(bits: Bits, batch: IrisBatch) -> ActorVitals | None:
    """Health / stamina of the creature in ``batch`` if it holds the 9-slot block described above, else None."""
    n, base = batch.data_bit_count, batch.data_bit_offset
    if n < _VITALS_BLOCK_BITS:
        return None
    big = bits.big
    for rel in range(0, n - _VITALS_BLOCK_BITS + 1):
        a = base + rel
        # cheap rejects first: the maxHealth slot (2nd) must have its flag bit set and its two floats equal
        if not (big >> (a + 66 + 32)) & 1:
            continue
        v1 = _slot(bits, a + 66)
        if v1 is None or v1 < _MIN_VITAL_MAX:
            continue
        v0 = _slot(bits, a)
        if v0 is None or v0 > v1 * 1.001:
            continue
        vals = [_slot(bits, a + 66 * i) for i in range(2, _VITALS_BLOCK_SLOTS)]
        if any(v is None for v in vals):
            continue
        stamina, max_stamina, again, half1, half2, again2, again3 = vals
        if (max_stamina < _MIN_VITAL_MAX or stamina > max_stamina * 1.001 or not _close(again, v1) or not _close(half1, v1 / 2)
                or not _close(half2, v1 / 2) or not _close(again2, v1) or not _close(again3, v1)):
            continue
        return ActorVitals(batch.net_ref_handle, v0, v1, stamina, max_stamina)
    return None




# ---------------------------------------------------------------- following health / stamina of OTHER creatures
#
# The 9-slot block above is sent rarely (a few times per minute at most, and then often without the max-stamina slot:
# 8 slots). Between two blocks a pawn keeps replicating its two changing attributes in small state batches, every
# second or so while it regenerates (179-bit batches: health at bit 47, stamina at bit 113; 224-bit: 92 / 158; the
# larger movement batches carry the same pair at other offsets). Health is always followed by stamina, 66 bits later.
# Evidence (hhh captures, pawn 402662, 80 s): health 2192.47, 2193.03, 2193.60, ... 2274.58 in steps of 0.56 per
# 0.75 s, stamina 533.8 ... 661 next to it, and the max values from the 8-slot block (2620.49) arrive 50 s later.
# So the pair is followed by continuity: a slot is health when it is close to the last health; once an offset has
# been confirmed twice for a batch header, any plausible value at that offset is accepted (a bite is a big jump).
# A percentage needs the max, so nothing is shown until the first block of the pawn.

_SLOT_BITS = 66
_FOLLOW_HEALTH_TOL = 0.10  # of maxHealth: how far a health slot may move between two batches and still be "the same slot"
_FOLLOW_STAMINA_TOL = 0.35
_FOLLOW_SINGLE_TOL = 0.03
_FOLLOW_TRUST_VOTES = 2


def attribute_slots(bits: Bits, batch: IrisBatch) -> list[tuple[int, float]]:
    """(offset relative to the batch, value) of every GAS attribute slot (float, flag bit 1, the same float again)."""
    n = batch.data_bit_count
    if n < 65:
        return []
    chunk = (bits.big >> batch.data_bit_offset) & mask(n)
    flags = (chunk >> 32) & mask(n - 64)
    found: list[tuple[int, float]] = []
    while flags:
        low = flags & -flags
        flags ^= low
        rel = low.bit_length() - 1
        raw = (chunk >> rel) & 0xFFFFFFFF
        if raw != (chunk >> (rel + 33)) & 0xFFFFFFFF:
            continue
        value = f32(raw)
        if math.isfinite(value) and 0.0 <= value <= 1_000_000.0:
            found.append((rel, value))
    return found


# ---------------------------------------------------------------- maxHealth in the creation record
#
# The creation record of a creature carries its initial attribute state, not as the 66-bit slots of the state batches
# but as floats 34 bits apart (value + two flag bits). The derived maxima are among them, and they have a shape no
# other data has: maxHealth twice in a row and maxHealth / 2 four times in a row (402662: 2606.108, 2606.108, 1303.054
# x4 at bits 3304..3474; its first block 50 s later said 2620.49, the creature had grown). Health itself and the
# stamina maxima are NOT in it, nor are growth, hunger and thirst. Seen in every pawn creation of the recorded
# sessions (Allosaurus, Deinosuchus and the class of protocol 536511885); the layout around it is class specific
# (the M pair and the M/2 quadruple may be far apart), so the two are searched for separately.

_CREATION_FLOAT_STRIDE = 34
_CREATION_MIN_BITS = 1500
_CREATION_MAX_RUN = 4


def creation_max_health(bits: Bits, batch: IrisBatch) -> float | None:
    """maxHealth read from a creation record (None when it does not carry the maxHealth pair and the half quadruple)."""
    n = batch.data_bit_count
    if n < _CREATION_MIN_BITS or batch_kind(bits, batch) != KIND_CREATION:
        return None
    stride = _CREATION_FLOAT_STRIDE
    chunk = (bits.big >> batch.data_bit_offset) & mask(n)
    raw: list[int] = []
    x = chunk
    for _ in range(n - 31):
        raw.append(x & 0xFFFFFFFF)
        x >>= 1
    span = stride * (_CREATION_MAX_RUN - 1)
    pairs: dict[int, int] = {}  # raw float -> first offset of two equal floats `stride` apart
    quads: list[int] = []  # offsets of four equal floats `stride` apart
    for r in range(len(raw) - span):
        v = raw[r]
        if v == 0:
            continue
        if raw[r + stride] == v:
            pairs.setdefault(v, r)
            if raw[r + 2 * stride] == v and raw[r + 3 * stride] == v:
                quads.append(r)
    for r in quads:
        half = f32(raw[r])
        if not math.isfinite(half) or not _MIN_VITAL_MAX / 2 <= half <= 500_000.0:
            continue
        for value in pairs:
            full = f32(value)
            if math.isfinite(full) and _close(full, 2.0 * half):
                return full
    return None


@dataclass(slots=True)
class PawnVitals:
    net_ref_handle: int
    health: float | None = None
    max_health: float | None = None
    stamina: float | None = None
    max_stamina: float | None = None
    verified: bool = False  # a full block was seen: the maxima are real
    max_from_creation: bool = False  # max_health is the creation record's: it may be a little low for a growing creature

    @property
    def health_pct(self) -> float | None:
        if self.health is None or not self.max_health:
            return None
        return max(0.0, min(100.0, 100.0 * self.health / self.max_health))

    @property
    def stamina_pct(self) -> float | None:
        if self.stamina is None or not self.max_stamina:
            return None
        return max(0.0, min(100.0, 100.0 * self.stamina / self.max_stamina))


def _full_block_8(slots: dict[int, float]) -> tuple[float, float, float] | None:
    """(health, maxHealth, stamina) of the 8-slot block (the 9-slot block without the unchanged max stamina)."""
    for a, health in slots.items():
        v = [slots.get(a + _SLOT_BITS * i) for i in range(1, 8)]
        if any(x is None for x in v):
            continue
        max_health, stamina, again, half1, half2, again2, again3 = v
        if (max_health < _MIN_VITAL_MAX or health > max_health * 1.001 or stamina > 100_000.0 or not _close(again, max_health)
                or not _close(half1, max_health / 2) or not _close(half2, max_health / 2) or not _close(again2, max_health)
                or not _close(again3, max_health)):
            continue
        return health, max_health, stamina
    return None


class PawnVitalsTracker:
    """Health and stamina of other creatures from their blocks and from the pair that follows (see section above)."""

    def __init__(self) -> None:
        self._pawns: dict[int, PawnVitals] = {}
        self._trusted: dict[int, set[int]] = {}  # batch header -> offsets of a confirmed health slot
        self._votes: Counter = Counter()
        self._pending: dict[int, tuple[int, int, float]] = {}  # handle -> (header, offset, health) awaiting a second sighting
        self.blocks = 0
        self.followed = 0
        self.seeded = 0

    def reset(self) -> None:
        self._pawns.clear()
        self._trusted.clear()
        self._votes.clear()
        self._pending.clear()
        self.blocks = self.followed = self.seeded = 0

    def forget(self, handle: int) -> None:
        self._pawns.pop(handle, None)
        self._pending.pop(handle, None)

    def get(self, handle: int) -> PawnVitals | None:
        return self._pawns.get(handle)

    def note_creation(self, handle: int, max_health: float | None) -> None:
        """A creature's creation record carried its maxHealth: a pawn that never sends a full block can still get a
        percentage. Health is not in the record; it is taken from the first pair that is confirmed (see ``_bootstrap``)."""
        if max_health is None or max_health < _MIN_VITAL_MAX:
            return
        pawn = self._pawns.get(handle)
        if pawn is not None and pawn.verified:
            return
        pawn = self._pawns[handle] = PawnVitals(handle, max_health=max_health, max_from_creation=True)
        self._pending.pop(handle, None)

    def update(self, bits: Bits, batch: IrisBatch) -> PawnVitals | None:
        """The pawn's vitals if this batch changed them, else None."""
        handle = batch.net_ref_handle
        n = batch.data_bit_count
        if n < 65:
            return None
        pawn = self._pawns.get(handle)
        if n >= _VITALS_BLOCK_BITS:
            block = decode_vitals_block(bits, batch)
            if block is not None:
                pawn = self._pawns.setdefault(handle, PawnVitals(handle))
                pawn.health, pawn.max_health, pawn.stamina, pawn.max_stamina = block.health, block.max_health, block.stamina, block.max_stamina
                pawn.verified, pawn.max_from_creation = True, False
                self.blocks += 1
                return pawn
        slots = dict(attribute_slots(bits, batch))
        if not slots:
            return None
        if n >= 8 * _SLOT_BITS:
            full = _full_block_8(slots)
            if full is not None:
                pawn = self._pawns.setdefault(handle, PawnVitals(handle))
                pawn.health, pawn.max_health, pawn.stamina = full
                pawn.verified, pawn.max_from_creation = True, False
                self.blocks += 1
                return pawn
        if pawn is None or not pawn.max_health or not (pawn.verified or pawn.max_from_creation):
            return None
        if pawn.health is None:
            return self._bootstrap(bits, batch, pawn, slots)
        return self._follow(bits, batch, pawn, slots)

    def _bootstrap(self, bits: Bits, batch: IrisBatch, pawn: PawnVitals, slots: dict[int, float]) -> PawnVitals | None:
        """First health of a pawn whose maxHealth came from its creation record: a health + stamina pair (66 bits apart)
        with health inside (2 %, 105 %] of the maximum, seen at the same place of the same batch layout twice with a
        health that moved less than 10 % of the maximum in between, or at an offset already confirmed on another pawn."""
        max_h, handle = pawn.max_health, pawn.net_ref_handle
        header = bits.read(batch.data_bit_offset, ACTOR_STATE_HEADER_BITS)
        trusted = self._trusted.get(header, ())
        for rel, health in slots.items():
            stamina = slots.get(rel + _SLOT_BITS)
            if stamina is None or not 0.02 * max_h <= health <= 1.05 * max_h or not _MIN_VITAL_MAX <= stamina <= 100_000.0:
                continue
            pend = self._pending.get(handle)
            if rel in trusted or (pend is not None and pend[:2] == (header, rel) and abs(pend[2] - health) <= _FOLLOW_HEALTH_TOL * max_h):
                self._pending.pop(handle, None)
                self._trusted.setdefault(header, set()).add(rel)  # two consistent sightings are what ``_follow`` asks for too
                pawn.health, pawn.stamina = health, stamina
                self.seeded += 1
                return pawn
            self._pending[handle] = (header, rel, health)
            return None
        return None

    def _follow(self, bits: Bits, batch: IrisBatch, pawn: PawnVitals, slots: dict[int, float]) -> PawnVitals | None:
        header = bits.read(batch.data_bit_offset, ACTOR_STATE_HEADER_BITS)
        max_h, max_s = pawn.max_health, pawn.max_stamina
        trusted = self._trusted.get(header, ())

        slack = 1.2 if pawn.max_from_creation else 1.001  # a growing creature passes the maximum its creation record gave

        def plausible_health(v: float) -> bool:
            return v <= max_h * slack

        def plausible_stamina(v: float) -> bool:
            if max_s is not None:
                return v <= max_s * 1.001
            # No maxStamina (8-slot form, e.g. a Deinosuchus): the slot 66 bits after the health may be another attribute, in
            # the gg capture the maxHealth that had just grown (stamina 997 -> 10644). Stamina regenerates slowly: it may
            # at most double (or reach a tenth of maxHealth) between two batches.
            return pawn.stamina is None or v <= max(2.0 * pawn.stamina, 0.1 * max_h)

        best = None  # (distance, rel, health, stamina | None)
        for rel, health in slots.items():
            if not plausible_health(health):
                continue
            stamina = slots.get(rel + _SLOT_BITS)
            if stamina is not None and not plausible_stamina(stamina):
                stamina = None
            near = abs(health - pawn.health) <= _FOLLOW_HEALTH_TOL * max_h
            if stamina is not None and pawn.stamina is not None and max_s:
                near = near and abs(stamina - pawn.stamina) <= _FOLLOW_STAMINA_TOL * max_s
            if rel in trusted and (stamina is not None or near):
                near = True
            elif stamina is None:
                near = near and abs(health - pawn.health) <= _FOLLOW_SINGLE_TOL * max_h
            if not near:
                continue
            distance = abs(health - pawn.health) / max_h
            if best is None or distance < best[0]:
                best = (distance, rel, health, stamina)
        if best is None:
            return None
        _, rel, health, stamina = best
        if rel not in trusted:
            self._votes[(header, rel)] += 1
            if self._votes[(header, rel)] >= _FOLLOW_TRUST_VOTES:
                self._trusted.setdefault(header, set()).add(rel)
        pawn.health = health
        if pawn.max_from_creation and health > pawn.max_health:
            pawn.max_health = health
        if stamina is not None:
            pawn.stamina = stamina
        self.followed += 1
        return pawn
