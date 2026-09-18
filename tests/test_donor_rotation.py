import os
import tempfile
import time
from state import BotState


def test_donor_rotation():
    state = BotState()
    assert state.current_donor_idx == 0
    assert state.rotations_count == 0

    idx1 = state.rotate_donor(donor_count=3, reason="Тестовая ротация 1")
    assert idx1 == 1
    assert state.current_donor_idx == 1
    assert state.rotations_count == 1

    idx2 = state.rotate_donor(donor_count=3, reason="Тестовая ротация 2")
    assert idx2 == 2
    assert state.current_donor_idx == 2
    assert state.rotations_count == 2

    # Замыкание круга
    idx3 = state.rotate_donor(donor_count=3, reason="Тестовая ротация 3")
    assert idx3 == 0
    assert state.current_donor_idx == 0
    assert state.rotations_count == 3


def test_state_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test_state.json")
        state1 = BotState(state_file_path=state_file)
        state1.record_check()
        state1.record_check()
        state1.record_ban_429()
        state1.record_slot("Астана")
        state1.rotate_donor(donor_count=2)

        # Проверяем, что файл записан
        assert os.path.exists(state_file)

        # Загружаем в новый объект
        state2 = BotState.load(state_file, donor_count=2)
        assert state2.checks_count == 2
        assert state2.bans_429_count == 1
        assert len(state2.slots_found_history) == 1
        assert "Астана" in state2.slots_found_history[0]
        assert state2.current_donor_idx == 1


def test_state_pause():
    state = BotState()
    assert not state.is_currently_paused()

    state.set_pause(minutes=10, reason="Слот найден")
    assert state.is_currently_paused()
    assert state.get_remaining_pause_seconds() > 0

    state.resume()
    assert not state.is_currently_paused()
    assert state.get_remaining_pause_seconds() == 0.0
