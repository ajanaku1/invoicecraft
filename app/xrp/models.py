"""Fail-closed, guided-only recovery classification for the XRPL/FSA path."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import PartyInput


class RecoveryClassification(str, Enum):
    PRE_PAYMENT_SAFE_RETRY = "pre_payment_safe_retry"
    XRPL_REJECTED_NO_FUNDS_MOVED = "xrpl_rejected_no_funds_moved"
    FLARE_EXECUTION_PENDING_OR_DELAYED = "flare_execution_pending_or_delayed"
    FLARE_REVERTED_SKIP_MEMO_GUIDANCE = "flare_reverted_skip_memo_guidance"
    FULLY_SETTLED_NO_RECOVERY = "fully_settled_no_recovery"
    IRREVERSIBLE_WRONG_DESTINATION = "irreversible_wrong_destination"
    IRREVERSIBLE_BELOW_MINIMUM_FEE = "irreversible_below_minimum_fee"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"

    @property
    def guidance(self) -> str:
        return _RECOVERY_GUIDANCE[self]


@dataclass(frozen=True)
class RecoveryObservation:
    """Only independently observed facts may drive a recovery classification."""

    payment_attempted: bool = False
    xrpl_validated: bool | None = None
    xrpl_success: bool | None = None
    destination_is_core_vault: bool | None = None
    meets_minimum_fee: bool | None = None
    flare_execution_status: Literal["pending", "delayed", "reverted", "succeeded"] | None = None


def classify_recovery(observation: RecoveryObservation) -> RecoveryClassification:
    """Classify explicit states; ambiguity always remains a manual-review case."""
    if not observation.payment_attempted:
        return _classify_pre_payment(observation)
    if _has_contradictory_downstream_facts(observation):
        return RecoveryClassification.MANUAL_REVIEW_REQUIRED
    if observation.xrpl_validated is not True:
        return RecoveryClassification.MANUAL_REVIEW_REQUIRED
    if observation.xrpl_success is False:
        return RecoveryClassification.XRPL_REJECTED_NO_FUNDS_MOVED
    if observation.xrpl_success is not True:
        return RecoveryClassification.MANUAL_REVIEW_REQUIRED
    return _classify_finalized_payment(observation)


def _has_contradictory_downstream_facts(observation: RecoveryObservation) -> bool:
    if observation.xrpl_success is not None and observation.xrpl_validated is not True:
        return True
    if observation.xrpl_success is False:
        return any(
            fact is not None
            for fact in (
                observation.destination_is_core_vault,
                observation.meets_minimum_fee,
                observation.flare_execution_status,
            )
        )
    return observation.flare_execution_status is not None and (
        observation.destination_is_core_vault is False or observation.meets_minimum_fee is False
    )


def _classify_pre_payment(observation: RecoveryObservation) -> RecoveryClassification:
    downstream_facts = (
        observation.xrpl_validated,
        observation.xrpl_success,
        observation.destination_is_core_vault,
        observation.meets_minimum_fee,
        observation.flare_execution_status,
    )
    if any(fact is not None for fact in downstream_facts):
        return RecoveryClassification.MANUAL_REVIEW_REQUIRED
    return RecoveryClassification.PRE_PAYMENT_SAFE_RETRY


def _classify_finalized_payment(observation: RecoveryObservation) -> RecoveryClassification:
    if observation.destination_is_core_vault is False:
        return RecoveryClassification.IRREVERSIBLE_WRONG_DESTINATION
    if observation.destination_is_core_vault is not True:
        return RecoveryClassification.MANUAL_REVIEW_REQUIRED
    if observation.meets_minimum_fee is False:
        return RecoveryClassification.IRREVERSIBLE_BELOW_MINIMUM_FEE
    if observation.meets_minimum_fee is not True:
        return RecoveryClassification.MANUAL_REVIEW_REQUIRED
    return _classify_flare_status(observation.flare_execution_status)


def _classify_flare_status(status: str | None) -> RecoveryClassification:
    if status in {"pending", "delayed"}:
        return RecoveryClassification.FLARE_EXECUTION_PENDING_OR_DELAYED
    if status == "reverted":
        return RecoveryClassification.FLARE_REVERTED_SKIP_MEMO_GUIDANCE
    if status == "succeeded":
        return RecoveryClassification.FULLY_SETTLED_NO_RECOVERY
    return RecoveryClassification.MANUAL_REVIEW_REQUIRED


_RECOVERY_GUIDANCE = {
    RecoveryClassification.PRE_PAYMENT_SAFE_RETRY: "No XRPL payment was attempted; rebuild and sign only after rechecking live values.",
    RecoveryClassification.XRPL_REJECTED_NO_FUNDS_MOVED: "The validated XRPL payment principal was rejected; the XRPL network fee may still be charged, so correct the cause before a new payment.",
    RecoveryClassification.FLARE_EXECUTION_PENDING_OR_DELAYED: "Wait for the attestation or delay window, then verify the same payment before retrying execution.",
    RecoveryClassification.FLARE_REVERTED_SKIP_MEMO_GUIDANCE: "Submit a guided 0xE0 skip-memo recovery after verifying the transaction.",
    RecoveryClassification.FULLY_SETTLED_NO_RECOVERY: "Settlement is evidenced; no recovery action is indicated.",
    RecoveryClassification.IRREVERSIBLE_WRONG_DESTINATION: "The payment destination is not the Core Vault; do not attempt automated recovery.",
    RecoveryClassification.IRREVERSIBLE_BELOW_MINIMUM_FEE: "The payment is below the live minimum or fee requirement; do not attempt automated recovery.",
    RecoveryClassification.MANUAL_REVIEW_REQUIRED: "Evidence is incomplete or conflicting; do not move funds automatically and request manual review.",
}


class XrpInvoiceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=10, max_length=2000)
    beneficiary: str
    issuer: PartyInput | None = None
    client: PartyInput | None = None
    currency: str = "USD"
    tax_rate: str = "0"

    @field_validator("beneficiary")
    @classmethod
    def validate_beneficiary(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.startswith("0x") or len(normalized) != 42:
            raise ValueError("beneficiary must be a 20-byte EVM address")
        try:
            raw = bytes.fromhex(normalized[2:])
        except ValueError as error:
            raise ValueError("beneficiary must be a 20-byte EVM address") from error
        if raw == b"\0" * 20:
            raise ValueError("beneficiary cannot be the zero address")
        return normalized

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"USD", "USD₮0"}:
            raise ValueError("currency must be USD or USD₮0")
        return normalized

    @field_validator("tax_rate")
    @classmethod
    def validate_tax_rate(cls, value: str) -> str:
        try:
            rate = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("tax_rate must be a decimal from 0 to 1") from error
        if not 0 <= rate <= 1:
            raise ValueError("tax_rate must be a decimal from 0 to 1")
        return str(rate)


class XrpSigningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_account: str = Field(min_length=1, max_length=64)


class XrpSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xrpl_transaction_hash: str = Field(min_length=64, max_length=66)

    @field_validator("xrpl_transaction_hash")
    @classmethod
    def validate_transaction_hash(cls, value: str) -> str:
        normalized = value.removeprefix("0x").removeprefix("0X")
        if len(normalized) != 64:
            raise ValueError("xrpl_transaction_hash must be 32-byte hex")
        try:
            bytes.fromhex(normalized)
        except ValueError as error:
            raise ValueError("xrpl_transaction_hash must be 32-byte hex") from error
        return normalized.upper()


class OperatorTransactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_hash: str = Field(min_length=66, max_length=66)

    @field_validator("transaction_hash")
    @classmethod
    def validate_transaction_hash(cls, value: str) -> str:
        if not value.startswith("0x"):
            raise ValueError("transaction_hash must be 0x-prefixed 32-byte hex")
        try:
            raw = bytes.fromhex(value[2:])
        except ValueError as error:
            raise ValueError("transaction_hash must be 0x-prefixed 32-byte hex") from error
        if len(raw) != 32:
            raise ValueError("transaction_hash must be 0x-prefixed 32-byte hex")
        return "0x" + raw.hex()
