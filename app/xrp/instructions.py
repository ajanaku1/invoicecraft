"""Exact-byte builders for Flare Smart Accounts 0xFE memo instructions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

MEMO_LENGTH = 42
CUSTOM_INSTRUCTION_ID = 0xFE


class InstructionError(ValueError):
    """Raised when an instruction cannot be safely constructed or inspected."""


@dataclass(frozen=True)
class CustomInstruction:
    """The preserved ABI bytes and their fixed-size XRPL memo commitment."""

    wallet_id: int
    executor_fee_uba: int
    packed_user_operation: bytes
    user_op_hash: bytes
    memo_bytes: bytes

    @property
    def memo_data_hex(self) -> str:
        return self.memo_bytes.hex().upper()


def build_custom_instruction(
    wallet_id: int, executor_fee_uba: int, packed_user_operation: bytes
) -> CustomInstruction:
    """Commit exact pre-encoded ABI bytes without attempting to re-encode them."""
    _validate_instruction_inputs(wallet_id, executor_fee_uba, packed_user_operation)
    user_op_hash = keccak256(packed_user_operation)
    memo = bytes([CUSTOM_INSTRUCTION_ID, wallet_id]) + executor_fee_uba.to_bytes(8, "big") + user_op_hash
    return CustomInstruction(wallet_id, executor_fee_uba, packed_user_operation, user_op_hash, memo)


def inspect_custom_instruction(memo_bytes: bytes, packed_user_operation: bytes) -> CustomInstruction:
    """Decode and verify a 0xFE commitment against exact supplied ABI bytes."""
    if not isinstance(memo_bytes, bytes) or len(memo_bytes) != MEMO_LENGTH:
        raise InstructionError("memo must be exactly 42 bytes")
    if memo_bytes[0] != CUSTOM_INSTRUCTION_ID:
        raise InstructionError("memo opcode must be 0xFE")
    wallet_id = memo_bytes[1]
    executor_fee_uba = int.from_bytes(memo_bytes[2:10], "big")
    instruction = build_custom_instruction(wallet_id, executor_fee_uba, packed_user_operation)
    if memo_bytes[10:] != instruction.user_op_hash:
        raise InstructionError("memo hash does not match the supplied PackedUserOperation")
    if memo_bytes != instruction.memo_bytes:
        raise InstructionError("memo layout is invalid")
    return instruction


def build_unsigned_payment(
    source_account: str,
    core_vault_destination: str,
    amount_drops: int,
    instruction: CustomInstruction,
) -> dict[str, object]:
    """Build an unsigned, unsubmitted XRPL Payment with no destination tag."""
    if not isinstance(source_account, str) or not source_account.strip():
        raise InstructionError("source account is required")
    if not isinstance(core_vault_destination, str) or not core_vault_destination.strip():
        raise InstructionError("live Core Vault destination is required")
    if isinstance(amount_drops, bool) or not isinstance(amount_drops, int) or amount_drops <= 0:
        raise InstructionError("amount in drops must be a positive integer")
    inspect_custom_instruction(instruction.memo_bytes, instruction.packed_user_operation)
    return {
        "TransactionType": "Payment",
        "Account": source_account,
        "Destination": core_vault_destination,
        "Amount": str(amount_drops),
        "Memos": [{"Memo": {"MemoData": instruction.memo_data_hex}}],
    }


def build_contract_user_operation(
    sender: str, nonce: int, target: str, contract_calldata: bytes
) -> bytes:
    """Encode one zero-value FSA executeUserOp call as a PackedUserOperation."""
    return build_contract_user_operations(
        sender, nonce, ((target, contract_calldata),)
    )


def build_contract_user_operations(
    sender: str,
    nonce: int,
    calls: Sequence[tuple[str, bytes]],
) -> bytes:
    """Encode ordered zero-value calls in one FSA PackedUserOperation."""
    sender_word = _evm_address_word(sender, "personal account")
    if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce < 0:
        raise InstructionError("nonce must be a nonnegative integer")
    encoded = tuple(_encode_contract_call(target, calldata) for target, calldata in calls)
    if not encoded:
        raise InstructionError("contract calls must be non-empty")
    offsets: list[bytes] = []
    offset = 32 * len(encoded)
    for call in encoded:
        offsets.append(_word(offset))
        offset += len(call)
    call_array = _word(len(encoded)) + b"".join(offsets) + b"".join(encoded)
    execute = keccak256(b"executeUserOp((address,uint256,bytes)[])")[:4] + _word(32) + call_array
    return _packed_user_operation(sender_word, nonce, execute)


def _encode_contract_call(target: str, calldata: bytes) -> bytes:
    target_word = _evm_address_word(target, "contract target")
    if not isinstance(calldata, bytes) or len(calldata) < 4:
        raise InstructionError("contract calldata must include a selector")
    return target_word + _word(0) + _word(96) + _abi_bytes(calldata)


def _validate_instruction_inputs(wallet_id: int, executor_fee_uba: int, payload: bytes) -> None:
    if isinstance(wallet_id, bool) or not isinstance(wallet_id, int) or not 0 <= wallet_id <= 0xFF:
        raise InstructionError("wallet ID must be a uint8")
    if isinstance(executor_fee_uba, bool) or not isinstance(executor_fee_uba, int):
        raise InstructionError("executor fee must be a uint64")
    if not 0 <= executor_fee_uba <= 0xFFFFFFFFFFFFFFFF:
        raise InstructionError("executor fee must be a uint64")
    if not isinstance(payload, bytes) or not payload:
        raise InstructionError("PackedUserOperation bytes must be non-empty")


def _packed_user_operation(sender: bytes, nonce: int, call_data: bytes) -> bytes:
    dynamic = (b"", call_data, b"", b"")
    offsets: list[int] = []
    offset = 9 * 32
    for value in dynamic:
        offsets.append(offset)
        offset += len(_abi_bytes(value))
    head = (
        sender,
        _word(nonce),
        _word(offsets[0]),
        _word(offsets[1]),
        _word(0),
        _word(0),
        _word(0),
        _word(offsets[2]),
        _word(offsets[3]),
    )
    return _word(32) + b"".join(head) + b"".join(_abi_bytes(value) for value in dynamic)


def _evm_address_word(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise InstructionError(f"{label} must be a 20-byte EVM address")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as error:
        raise InstructionError(f"{label} must be a 20-byte EVM address") from error
    if raw == b"\0" * 20:
        raise InstructionError(f"{label} cannot be the zero address")
    return b"\0" * 12 + raw


def _abi_bytes(value: bytes) -> bytes:
    return _word(len(value)) + value + b"\0" * ((-len(value)) % 32)


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def keccak256(payload: bytes) -> bytes:
    """Return Ethereum Keccak-256 without substituting NIST SHA3-256."""
    state = [0] * 25
    padded = bytearray(payload)
    padding_length = 136 - len(padded) % 136
    if padding_length == 1:
        padded.append(0x81)
    else:
        padded.append(0x01)
        padded.extend(b"\x00" * (padding_length - 2))
        padded.append(0x80)
    for offset in range(0, len(padded), 136):
        for lane in range(17):
            state[lane] ^= int.from_bytes(padded[offset + lane * 8 : offset + lane * 8 + 8], "little")
        _keccak_f1600(state)
    return b"".join(word.to_bytes(8, "little") for word in state)[:32]


def _keccak_f1600(state: list[int]) -> None:
    mask = (1 << 64) - 1
    for round_constant in _ROUND_CONSTANTS:
        columns = [state[index] ^ state[index + 5] ^ state[index + 10] ^ state[index + 15] ^ state[index + 20] for index in range(5)]
        for x in range(5):
            adjustment = columns[(x - 1) % 5] ^ _rotate_left(columns[(x + 1) % 5], 1, mask)
            for y in range(5):
                state[x + 5 * y] ^= adjustment
        rotated = [0] * 25
        for x in range(5):
            for y in range(5):
                rotated[y + 5 * ((2 * x + 3 * y) % 5)] = _rotate_left(state[x + 5 * y], _ROTATIONS[x][y], mask)
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = rotated[x + 5 * y] ^ ((~rotated[(x + 1) % 5 + 5 * y]) & rotated[(x + 2) % 5 + 5 * y])
        state[0] ^= round_constant


def _rotate_left(value: int, amount: int, mask: int) -> int:
    if amount == 0:
        return value
    return ((value << amount) | (value >> (64 - amount))) & mask


_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
