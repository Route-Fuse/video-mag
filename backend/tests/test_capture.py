import time
import pytest
from app.capture import Session


def test_demo_produces_both_previews_and_stops_and_restarts():
    session = Session()
    try:
        for mode in ('color', 'motion'):
            session.configure(dict(mode=mode, gain=10, low=.7, high=3, fps=30))
            session.start('demo')
            deadline = time.monotonic() + 3
            while session.status()['frames'] < 5 and time.monotonic() < deadline:
                time.sleep(.02)
            assert session.status()['state'] == 'running'
            assert session.status()['frames'] >= 5
            for frame in session.frames.values():
                assert frame.startswith(b'\xff\xd8')
            with pytest.raises(ValueError, match='Stop'):
                session.start('demo')
            session.stop()
            assert not session.worker.is_alive()
            assert session.status()['state'] == 'idle'
    finally:
        session.stop()
