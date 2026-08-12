from __future__ import annotations

import pytest

from app.xrp.models import RecoveryClassification, RecoveryObservation, classify_recovery


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (RecoveryObservation(), RecoveryClassification.PRE_PAYMENT_SAFE_RETRY),
        (
            RecoveryObservation(payment_attempted=True, xrpl_validated=True, xrpl_success=False),
            RecoveryClassification.XRPL_REJECTED_NO_FUNDS_MOVED,
        ),
        (
            RecoveryObservation(
                payment_attempted=True,
                xrpl_validated=True,
                xrpl_success=True,
                destination_is_core_vault=True,
                meets_minimum_fee=True,
                flare_execution_status="delayed",
            ),
            RecoveryClassification.FLARE_EXECUTION_PENDING_OR_DELAYED,
        ),
        (
            RecoveryObservation(
                payment_attempted=True,
                xrpl_validated=True,
                xrpl_success=True,
                destination_is_core_vault=True,
                meets_minimum_fee=True,
                flare_execution_status="reverted",
            ),
            RecoveryClassification.FLARE_REVERTED_SKIP_MEMO_GUIDANCE,
        ),
        (
            RecoveryObservation(
                payment_attempted=True,
                xrpl_validated=True,
                xrpl_success=True,
                destination_is_core_vault=True,
                meets_minimum_fee=True,
                flare_execution_status="succeeded",
            ),
            RecoveryClassification.FULLY_SETTLED_NO_RECOVERY,
        ),
        (
            RecoveryObservation(
                payment_attempted=True,
                xrpl_validated=True,
                xrpl_success=True,
                destination_is_core_vault=False,
            ),
            RecoveryClassification.IRREVERSIBLE_WRONG_DESTINATION,
        ),
        (
            RecoveryObservation(
                payment_attempted=True,
                xrpl_validated=True,
                xrpl_success=True,
                destination_is_core_vault=True,
                meets_minimum_fee=False,
            ),
            RecoveryClassification.IRREVERSIBLE_BELOW_MINIMUM_FEE,
        ),
    ],
)
def test_classifies_only_explicitly_observed_recovery_states(
    observation: RecoveryObservation, expected: RecoveryClassification
) -> None:
    assert classify_recovery(observation) is expected


def test_unknown_or_conflicting_facts_fail_closed_to_manual_review() -> None:
    observation = RecoveryObservation(
        payment_attempted=True,
        xrpl_validated=False,
        xrpl_success=True,
        destination_is_core_vault=True,
        meets_minimum_fee=True,
        flare_execution_status="succeeded",
    )

    assert classify_recovery(observation) is RecoveryClassification.MANUAL_REVIEW_REQUIRED


@pytest.mark.parametrize(
    "observation",
    [
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=True,
            xrpl_success=False,
            destination_is_core_vault=True,
        ),
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=True,
            xrpl_success=False,
            meets_minimum_fee=True,
        ),
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=True,
            xrpl_success=False,
            flare_execution_status="reverted",
        ),
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=True,
            xrpl_success=True,
            destination_is_core_vault=False,
            flare_execution_status="succeeded",
        ),
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=True,
            xrpl_success=True,
            destination_is_core_vault=True,
            meets_minimum_fee=False,
            flare_execution_status="pending",
        ),
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=False,
            xrpl_success=False,
            destination_is_core_vault=True,
        ),
    ],
)
def test_contradictory_downstream_recovery_observations_require_manual_review(
    observation: RecoveryObservation,
) -> None:
    assert classify_recovery(observation) is RecoveryClassification.MANUAL_REVIEW_REQUIRED


@pytest.mark.parametrize(
    "observation",
    [
        RecoveryObservation(xrpl_validated=True),
        RecoveryObservation(xrpl_success=False),
        RecoveryObservation(destination_is_core_vault=True),
        RecoveryObservation(meets_minimum_fee=True),
        RecoveryObservation(flare_execution_status="succeeded"),
    ],
)
def test_pre_payment_observation_with_downstream_fact_requires_manual_review(
    observation: RecoveryObservation,
) -> None:
    assert classify_recovery(observation) is RecoveryClassification.MANUAL_REVIEW_REQUIRED


def test_recovery_guidance_never_claims_an_automatic_fund_movement() -> None:
    classification = classify_recovery(
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=True,
            xrpl_success=True,
            destination_is_core_vault=True,
            meets_minimum_fee=True,
            flare_execution_status="reverted",
        )
    )

    assert classification.guidance == "Submit a guided 0xE0 skip-memo recovery after verifying the transaction."
    assert "automatic" not in classification.guidance.lower()


def test_xrpl_rejection_guidance_accounts_for_the_network_fee() -> None:
    classification = classify_recovery(
        RecoveryObservation(payment_attempted=True, xrpl_validated=True, xrpl_success=False)
    )

    assert "principal" in classification.guidance.lower()
    assert "network fee" in classification.guidance.lower()
