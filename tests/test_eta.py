from xp12dem.eta import CACHED_DOWNLOAD, DEFAULT_DOWNLOAD, Estimator, TimingHistory, build_key


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_remaining_counts_down_with_time():
    clock = Clock()
    tiles = [(47, 10), (47, 11), (47, 12)]
    eta = Estimator(tiles, cached={(47, 10)}, workers=1, expected_build=100, clock=clock)
    first = eta.remaining()
    assert abs(first - (CACHED_DOWNLOAD + 2 * DEFAULT_DOWNLOAD + 300 + 7)) < 1e-6
    previous = first
    for tile in tiles:  # downloads
        eta.download_started(tile)
        clock.now += 0.2 if tile == (47, 10) else 20
        eta.download_finished(tile)
    for tile in tiles:  # builds, each as long as expected
        eta.build_started(tile)
        for _ in range(10):
            clock.now += 10
            now = eta.remaining()
            assert now <= previous + 1e-6, "estimate must not grow when things go as expected"
            previous = now
        eta.build_finished(tile, ok=True)
    eta.final_done()
    assert eta.remaining() < 5


def test_measured_build_time_replaces_guess():
    clock = Clock()
    eta = Estimator([(1, 1), (1, 2), (1, 3)], cached={(1, 1), (1, 2), (1, 3)}, workers=1,
                    expected_build=100, clock=clock)
    for tile in [(1, 1), (1, 2), (1, 3)]:
        eta.download_started(tile)
        eta.download_finished(tile)
    for tile in [(1, 1), (1, 2)]:
        eta.build_started(tile)
        clock.now += 300
        eta.build_finished(tile, ok=True)
    assert eta.per_tile_build() == 300
    assert 300 <= eta.remaining() <= 320


def test_parallel_workers_share_the_work():
    eta = Estimator([(1, i) for i in range(4)], cached={(1, i) for i in range(4)}, workers=4,
                    expected_build=100, clock=Clock())
    for tile in [(1, i) for i in range(4)]:
        eta.download_started(tile)
        eta.download_finished(tile)
    assert eta.remaining() < 120


def test_history_is_remembered(tmp_path):
    path = tmp_path / "timings.json"
    history = TimingHistory(path)
    key = build_key("rebuild", "1", "high")
    assert key == "rebuild-1-high" and build_key("improve", "1", "high") == "improve-1"
    for seconds in (100, 300, 200):
        history.add(key, seconds)
    history.save()
    assert TimingHistory(path).typical(key) == 200
    assert TimingHistory(path).typical("other") is None
