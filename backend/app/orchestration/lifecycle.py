"""Session state machine (README: Requested -> Provisioning -> Ready -> Active -> Quiescing ->
Archived -> Destroyed, plus Failed). Illegal transitions raise instead of silently corrupting state."""
from app.models import SessionStatus as S

TRANSITIONS: dict[str, set[str]] = {
    S.requested: {S.provisioning, S.failed},
    S.provisioning: {S.ready, S.failed},
    S.ready: {S.active, S.quiescing, S.failed},
    S.active: {S.quiescing, S.failed},
    S.quiescing: {S.archived, S.destroyed, S.failed},
    S.archived: {S.destroyed},
    S.failed: {S.destroyed},
    S.destroyed: set(),
}
LIVE = {S.provisioning, S.ready, S.active}
TERMINAL = {S.destroyed}


class IllegalTransition(Exception):
    pass


def check(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(S(current), set()):
        raise IllegalTransition(f"{current} -> {new}")
