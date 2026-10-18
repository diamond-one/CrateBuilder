"""Small, transactional editing operations for selected pads and physical banks.

Bank protection is independent of individual pad locks. Unlocking a bank keeps
the pad locks that were already set before the bank was protected.
"""
import copy
import operator

from .matching import Matcher


def _index(value, count, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer index.")
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be an integer index.") from exc
    if not 0 <= result < count:
        raise ValueError(f"{label} is outside this session.")
    return result


def regenerate_indices(session, indices):
    """Mutate and return this session, rebuilding only selected unlocked pads.

    Outside assignments (including their warning/reason text and object identity)
    and all original lock states are preserved. One effective operation advances
    the seed once. Empty or entirely locked selections are no-ops. Building runs
    on an isolated copy, so a failure leaves assignments, seed and history intact.
    The current shared builder is ``Matcher.build``; it retains the existing
    role matching and history rules for both eight- and sixteen-pad sessions.
    """
    try:
        requested = sorted({_index(value, len(session.pads), "Pad index") for value in indices})
    except TypeError as exc:
        raise ValueError("Pad indices must be an iterable of integer indices.") from exc
    selected = [index for index in requested if not session.pads[index].locked
                and index // 16 not in session.locked_banks]
    if not selected:
        return session
    selected_set = set(selected)
    original_locks = [pad.locked for pad in session.pads]
    working = copy.copy(session)
    working.pads = copy.deepcopy(session.pads)
    working.history = list(session.history)
    working.locked_banks = list(session.locked_banks)
    working.generation_sources = list(session.generation_sources) if session.generation_sources is not None else None
    working.seed = session.seed + 1
    for index, pad in enumerate(working.pads):
        if index not in selected_set:
            pad.locked = True
    # Temporary locks allow the shared kit builder to keep all musical context
    # while leaving every unselected pad out of its choice/history operations.
    result = Matcher(working).build()
    for index in selected:
        result.pads[index].locked = original_locks[index]
    for index in selected:
        session.pads[index] = result.pads[index]
    session.seed, session.history = result.seed, list(result.history)
    # Prepared immutable library statistics can safely be reused by the UI.
    if hasattr(result, "_matching_cache"):
        session._matching_cache = result._matching_cache
    return session


def _bank_indices(session, bank):
    bank = _index(bank, len(session.pads) // 16, "Bank index")
    return range(bank * 16, (bank + 1) * 16)


def bank_lock_state(session, bank):
    """Return effective 'all', 'some' or 'none', including independent bank protection."""
    indices = _bank_indices(session, bank)
    if bank in session.locked_banks:
        return "all"
    flags = [session.pads[index].locked for index in indices]
    return "all" if all(flags) else "some" if any(flags) else "none"


def set_bank_locked(session, bank, locked):
    """Mutate/return this session, changing only the independent bank-protection flag."""
    bank = _bank_indices(session, bank).start // 16
    if not isinstance(locked, bool):
        raise ValueError("The bank lock value must be True or False.")
    flags = set(session.locked_banks)
    if locked:
        flags.add(bank)
    else:
        flags.discard(bank)
    session.locked_banks = sorted(flags)
    return session
