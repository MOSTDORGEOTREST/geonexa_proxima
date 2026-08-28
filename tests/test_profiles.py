from datetime import UTC, datetime
from uuid import uuid4

from geonexa_proxima.domain import (
    InterestPolarity,
    InterestSignalSource,
    ProfileInterest,
    ProfileInterestSignal,
)
from geonexa_proxima.services.profiles import ProfileCompiler


def test_profile_compiler_is_stable_and_separates_learned_signals() -> None:
    now = datetime.now(UTC)
    profile_id = uuid4()
    interests = [
        ProfileInterest(
            id=uuid4(),
            profile_id=profile_id,
            query="computer vision",
            polarity=InterestPolarity.NEGATIVE,
            weight=3,
            created_at=now,
            updated_at=now,
        ),
        ProfileInterest(
            id=uuid4(),
            profile_id=profile_id,
            query="soil liquefaction",
            polarity=InterestPolarity.POSITIVE,
            weight=10,
            created_at=now,
            updated_at=now,
        ),
    ]
    signals = [
        ProfileInterestSignal(
            id=uuid4(),
            profile_id=profile_id,
            query="neural operator",
            polarity=InterestPolarity.POSITIVE,
            weight=2,
            source=InterestSignalSource.FEEDBACK,
            evidence_count=2,
            created_at=now,
            updated_at=now,
        )
    ]
    compiler = ProfileCompiler("Geotechnical engineering")

    first = compiler.compile_profile(
        description="ML for cyclic soil response",
        interests=interests,
        learned_signals=signals,
    )
    second = compiler.compile_profile(
        description="ML for cyclic soil response",
        interests=list(reversed(interests)),
        learned_signals=signals,
    )

    assert first == second
    assert "Explicit interests" in first
    assert "negative: computer vision" in first
    assert "Learned interest signals" in first
    assert "evidence=2" in first
