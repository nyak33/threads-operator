from threads_operator.insights import (
    detect_second_wave,
    growth_rate,
    sample_interval_minutes,
    velocity,
)


def test_sampling_boundaries():
    assert sample_interval_minutes(0) == 5
    assert sample_interval_minutes(119) == 5
    assert sample_interval_minutes(120) == 15
    assert sample_interval_minutes(359) == 15
    assert sample_interval_minutes(360) == 30
    assert sample_interval_minutes(1439) == 30
    assert sample_interval_minutes(1440) == 60
    assert sample_interval_minutes(4319) == 60
    assert sample_interval_minutes(4320) == 360
    assert sample_interval_minutes(10079) == 360
    assert sample_interval_minutes(10080) == 1440


def test_growth_rate_and_velocity_preserve_unknowns():
    assert growth_rate(200, 210) == 5.0
    assert growth_rate(0, 10) is None
    assert growth_rate(None, 10) is None
    assert growth_rate(200, None) is None
    assert velocity(100, 160, 5) == 12.0
    assert velocity(None, 160, 5) is None
    assert velocity(100, None, 5) is None
    assert velocity(100, 160, 0) is None


def test_second_wave_requires_slowdown_then_material_acceleration():
    assert detect_second_wave([(0, 0), (60, 600), (120, 780), (180, 840), (240, 1200)]) is True
    assert detect_second_wave([(0, 0), (60, 600), (120, 900), (180, 1100), (240, 1250)]) is False


def test_second_wave_needs_enough_valid_points():
    assert detect_second_wave([(0, 0), (60, 100)]) is False
    assert detect_second_wave([(0, None), (60, 100), (120, 200), (180, 300)]) is False
