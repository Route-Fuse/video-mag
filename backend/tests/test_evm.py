import cv2
import numpy as np
import pytest
from app.evm import Magnifier, TemporalBandpass


@pytest.mark.parametrize('mode', ['color', 'motion'])
def test_zero_gain_is_exact_identity(mode):
    frame = np.random.default_rng(4).integers(0, 256, (73, 97, 3), dtype=np.uint8)
    assert np.array_equal(Magnifier(mode=mode, gain=0).process(frame), frame)


@pytest.mark.parametrize('mode', ['color', 'motion'])
def test_static_scene_does_not_create_changes(mode):
    frame = np.random.default_rng(3).integers(20, 230, (73, 97, 3), dtype=np.uint8)
    processor = Magnifier(mode=mode, gain=40)
    for _ in range(60):
        assert np.array_equal(processor.process(frame), frame)


def amplitude(values, frequency, fps=30):
    values = np.asarray(values)
    t = np.arange(len(values)) / fps
    basis = np.column_stack((np.sin(2 * np.pi * frequency * t), np.cos(2 * np.pi * frequency * t), np.ones(len(t))))
    fit = np.linalg.lstsq(basis, values, rcond=None)[0]
    return np.hypot(fit[0], fit[1])


def test_temporal_filter_preserves_passband_and_suppresses_outside():
    def response(freq):
        filt = TemporalBandpass(1, 1.5, 30)
        output = [filt.apply(np.array([np.sin(2 * np.pi * freq * i / 30)]))[0] for i in range(600)]
        return amplitude(output[300:], freq)
    assert response(1.2) > .9
    assert response(.2) < .03
    assert response(7) < .03


def test_color_amplifies_known_weak_signal():
    processor = Magnifier(mode='color', gain=10, low=1, high=1.5)
    original, amplified = [], []
    for index in range(360):
        level = round(128 + 3 * np.sin(2 * np.pi * 1.2 * index / 30))
        frame = np.full((64, 96, 3), 128, np.uint8)
        frame[:, :, 1] = level
        result = processor.process(frame)
        original.append(frame[:, :, 1].mean())
        amplified.append(result[:, :, 1].mean())
    ratio = amplitude(amplified[180:], 1.2) / amplitude(original[180:], 1.2)
    assert 9 < ratio < 12


def test_motion_amplifies_known_subpixel_translation():
    width, height = 160, 96
    x = np.arange(width, dtype=np.float32)
    reference = 100 + 50 * np.exp(-((x - 80) / 7) ** 2)
    derivative = np.gradient(reference)[60:100]
    processor = Magnifier(mode='motion', gain=10, low=1, high=1.5)
    before, after = [], []
    for index in range(360):
        displacement = .4 * np.sin(2 * np.pi * 1.2 * index / 30)
        profile = 100 + 50 * np.exp(-((x - 80 - displacement) / 7) ** 2)
        frame = np.broadcast_to(np.rint(profile).astype(np.uint8)[None, :, None], (height, width, 3)).copy()
        output = processor.process(frame)
        before.append(np.dot(frame[48, 60:100, 0] - reference[60:100], derivative))
        after.append(np.dot(output[48, 60:100, 0] - reference[60:100], derivative))
    assert amplitude(after[180:], 1.2) > 2 * amplitude(before[180:], 1.2)


@pytest.mark.parametrize('low,high', [(0, 2), (3, 2), (1, 15)])
def test_invalid_frequency_bands_rejected(low, high):
    with pytest.raises(ValueError):
        Magnifier(low=low, high=high)
