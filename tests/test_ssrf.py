from __future__ import annotations

import asyncio
import ipaddress
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from app import service
from app.jobs import JobStore
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def store_and_root(tmp_path: Path) -> tuple[JobStore, Path]:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    store = JobStore(data / "jobs.db")
    return store, data


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://example.com/audio.mp3",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/plain;base64,SGVsbG8=",
        "gopher://example.com/1",
        "ws://example.com/socket",
        "http://",
        "https://",
    ],
)
def test_ssrf_rejects_invalid_scheme_and_empty_host(
    store_and_root: tuple[JobStore, Path], bad_url: str
) -> None:
    store, root = store_and_root
    status, body = service.create_url_job(store, root, bad_url, "fast")
    assert status == 400
    assert "error" in body
    # Ensure no directories or jobs created
    assert store.count_jobs() == 0
    jobs_dir = root / "jobs"
    if jobs_dir.exists():
        assert list(jobs_dir.iterdir()) == []


@pytest.mark.parametrize(
    "userinfo_url",
    [
        "http://user:pass@8.8.8.8/audio.mp3",
        "https://user@8.8.8.8/audio.mp3",
        "http://:pass@8.8.8.8/audio.mp3",
        "http://@8.8.8.8/audio.mp3",
    ],
)
def test_ssrf_rejects_userinfo(
    store_and_root: tuple[JobStore, Path], userinfo_url: str
) -> None:
    store, root = store_and_root
    status, body = service.create_url_job(store, root, userinfo_url, "fast")
    assert status == 400
    assert "url 不能包含用户信息" in body["error"]
    assert store.count_jobs() == 0


@pytest.mark.parametrize(
    "blocked_url",
    [
        # 回环
        "http://127.0.0.1/audio.mp3",
        "http://127.0.0.2/audio.mp3",
        # 私有网段
        "http://10.0.0.1/audio.mp3",
        "http://172.16.0.1/audio.mp3",
        "http://192.168.1.1/audio.mp3",
        # 链路本地 / 云元数据
        "http://169.254.169.254/latest/meta-data",
        # 100.64.0.0/10 共享段（本机 tailnet 必测样例）
        "http://100.64.0.99/audio.mp3",
        # IPv4 映射 IPv6
        "http://[::ffff:127.0.0.1]/audio.mp3",
        # 十进制整数主机名 (2130706433 == 127.0.0.1)
        "http://2130706433/audio.mp3",
        # 0.0.0.0
        "http://0.0.0.0/audio.mp3",
    ],
)
def test_ssrf_blocks_non_global_ips(
    store_and_root: tuple[JobStore, Path], blocked_url: str
) -> None:
    store, root = store_and_root
    status, body = service.create_url_job(store, root, blocked_url, "fast")
    assert status == 400
    assert body["error"] == "禁止访问非公网地址"
    assert store.count_jobs() == 0
    jobs_dir = root / "jobs"
    if jobs_dir.exists():
        assert list(jobs_dir.iterdir()) == []


def test_ssrf_injected_resolver_with_domain(
    store_and_root: tuple[JobStore, Path]
) -> None:
    store, root = store_and_root

    # Fake resolver returning non-global IP for custom domain
    def fake_private_resolver(
        host: str,
    ) -> Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if host == "internal.corp":
            return ["10.0.1.20"]
        if host == "tailscale.node":
            return [ipaddress.ip_address("100.64.0.99")]
        return []

    status1, body1 = service.create_url_job(
        store, root, "https://internal.corp/audio.mp3", "fast", resolver=fake_private_resolver
    )
    assert status1 == 400
    assert body1["error"] == "禁止访问非公网地址"

    status2, body2 = service.create_url_job(
        store, root, "https://tailscale.node/audio.mp3", "fast", resolver=fake_private_resolver
    )
    assert status2 == 400
    assert body2["error"] == "禁止访问非公网地址"

    # Fake resolver returning global IP allows job creation
    def fake_public_resolver(
        host: str,
    ) -> Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if host == "cdn.example.com":
            return [ipaddress.ip_address("93.184.216.34")]
        return []

    status3, body3 = service.create_url_job(
        store, root, "https://cdn.example.com/audio.mp3", "fast", resolver=fake_public_resolver
    )
    assert status3 == 200
    assert "job_id" in body3
    assert store.count_jobs() == 1


def test_ssrf_mixed_public_private_resolution_rejected(
    store_and_root: tuple[JobStore, Path],
) -> None:
    store, root = store_and_root

    def fake_mixed_resolver(
        host: str,
    ) -> Sequence[str | ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return ["93.184.216.34", "10.0.0.1"]

    status, body = service.create_url_job(
        store, root, "https://mixed.example.com/audio.mp3", "fast", resolver=fake_mixed_resolver
    )
    assert status == 400
    assert body["error"] == "禁止访问非公网地址"
    assert store.count_jobs() == 0


def test_ssrf_resolver_failures_return_400(
    store_and_root: tuple[JobStore, Path]
) -> None:
    store, root = store_and_root

    def empty_resolver(host: str) -> list[str]:
        return []

    def failing_resolver(host: str) -> list[str]:
        raise OSError("DNS lookup failed")

    s1, b1 = service.create_url_job(
        store, root, "https://unknown-domain.invalid/a.mp3", "fast", resolver=empty_resolver
    )
    assert s1 == 400
    assert b1["error"] == "url 主机名无法解析"

    s2, b2 = service.create_url_job(
        store, root, "https://unknown-domain.invalid/a.mp3", "fast", resolver=failing_resolver
    )
    assert s2 == 400
    assert b2["error"] == "url 主机名无法解析"
    assert store.count_jobs() == 0


def test_ssrf_public_ip_creates_job(store_and_root: tuple[JobStore, Path]) -> None:
    store, root = store_and_root
    # 8.8.8.8 and 1.1.1.1 are global
    status, body = service.create_url_job(
        store, root, "https://8.8.8.8/audio.mp3", "fast"
    )
    assert status == 200
    assert "job_id" in body
    assert store.count_jobs() == 1
    job = store.get(body["job_id"])
    assert job is not None
    assert job["url"] == "https://8.8.8.8/audio.mp3"


def test_ssrf_via_api_and_web_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    app = create_app(data)

    with TestClient(app) as http:
        # 1. API route POST /jobs
        resp = http.post(
            "/jobs",
            json={"url": "http://169.254.169.254/latest/meta-data", "tier": "fast"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "禁止访问非公网地址"

        # 2. Web form route POST /new
        form_resp = http.post(
            "/new",
            data={"tier": "fast", "url": "http://100.64.0.99/audio.mp3"},
        )
        assert form_resp.status_code == 400
        assert "禁止访问非公网地址" in form_resp.text


def test_dns_resolution_timeout_returns_400(
    store_and_root: tuple[JobStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root = store_and_root

    def slow_resolver(host: str) -> list[str]:
        time.sleep(0.3)
        return ["93.184.216.34"]

    # 1. 显式指定 dns_timeout 小于延迟
    status, body = service.create_url_job(
        store,
        root,
        "https://slow.example.com/audio.mp3",
        "fast",
        resolver=slow_resolver,
        dns_timeout=0.05,
    )
    assert status == 400
    assert body["error"] == "url 解析超时"
    assert store.count_jobs() == 0

    # 2. 通过全局具名常量配置生效
    monkeypatch.setattr(service, "DNS_TIMEOUT_SECONDS", 0.05)
    status2, body2 = service.create_url_job(
        store,
        root,
        "https://slow.example.com/audio.mp3",
        "fast",
        resolver=slow_resolver,
    )
    assert status2 == 400
    assert body2["error"] == "url 解析超时"
    assert store.count_jobs() == 0


def test_concurrent_request_not_blocked_during_dns_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    app = create_app(data)

    def slow_resolver(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        time.sleep(0.3)
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(service, "default_resolver", slow_resolver)

    async def run_concurrent() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            t0 = time.monotonic()
            fast_finished: float | None = None
            slow_finished: float | None = None

            async def call_create_job() -> httpx.Response:
                nonlocal slow_finished
                resp = await client.post(
                    "/jobs",
                    json={"url": "https://slow.example.com/audio.mp3", "tier": "fast"},
                )
                slow_finished = time.monotonic() - t0
                return resp

            async def call_health() -> httpx.Response:
                nonlocal fast_finished
                await asyncio.sleep(0.05)
                resp = await client.get("/health")
                fast_finished = time.monotonic() - t0
                return resp

            slow_resp, health_resp = await asyncio.gather(call_create_job(), call_health())

            assert health_resp.status_code == 200
            assert health_resp.json() == {"ok": True}
            assert slow_resp.status_code == 200
            assert "job_id" in slow_resp.json()

            assert fast_finished is not None and slow_finished is not None
            # 并发请求必须在慢解析完成前返回，证明 event loop 未被阻塞
            assert fast_finished < slow_finished
            assert fast_finished < 0.2
            assert slow_finished >= 0.28

            # 2. 验证 POST /new (Web 网页表单) 同样在线程池执行不阻塞
            t1 = time.monotonic()
            web_fast_finished: float | None = None
            web_slow_finished: float | None = None

            async def call_web_new() -> httpx.Response:
                nonlocal web_slow_finished
                resp = await client.post(
                    "/new",
                    data={"url": "https://slow.example.com/audio.mp3", "tier": "fast"},
                    follow_redirects=False,
                )
                web_slow_finished = time.monotonic() - t1
                return resp

            async def call_web_health() -> httpx.Response:
                nonlocal web_fast_finished
                await asyncio.sleep(0.05)
                resp = await client.get("/health")
                web_fast_finished = time.monotonic() - t1
                return resp

            web_slow_resp, web_health_resp = await asyncio.gather(call_web_new(), call_web_health())
            assert web_health_resp.status_code == 200
            assert web_slow_resp.status_code == 303
            assert web_fast_finished is not None and web_slow_finished is not None
            assert web_fast_finished < web_slow_finished
            assert web_fast_finished < 0.2
            assert web_slow_finished >= 0.28

    asyncio.run(run_concurrent())


def test_dns_resolution_concurrency_never_exceeds_eight() -> None:
    """同时发起超过 8 个解析时，假 resolver 里观测到的同时执行数不超过 8。"""
    current = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def resolver(host: str) -> list[str]:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        try:
            release.wait(timeout=2)
            return ["93.184.216.34"]
        finally:
            with lock:
                current -= 1

    errors: list[BaseException] = []

    def call() -> None:
        try:
            service._resolve_with_timeout(resolver, "example.com", timeout=2.0)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=call) for _ in range(12)]
    try:
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with lock:
                if peak >= 8:
                    break
            time.sleep(0.01)
        time.sleep(0.05)
        with lock:
            assert peak == 8
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=3)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []


def test_dns_resolution_times_out_when_gate_is_full() -> None:
    """闸被占满且本次预算耗尽时抛 TimeoutError，不挂死，也不进入新的 resolver。"""
    release = threading.Event()
    entered = 0
    lock = threading.Lock()

    def blocker(host: str) -> list[str]:
        nonlocal entered
        with lock:
            entered += 1
        release.wait(timeout=3)
        return ["93.184.216.34"]

    holders: list[threading.Thread] = []

    def hold() -> None:
        try:
            service._resolve_with_timeout(blocker, "held.example", timeout=3.0)
        except BaseException:
            pass

    try:
        for _ in range(8):
            worker = threading.Thread(target=hold)
            worker.start()
            holders.append(worker)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with lock:
                if entered >= 8:
                    break
            time.sleep(0.01)
        with lock:
            assert entered == 8

        started = 0

        def ninth(host: str) -> list[str]:
            nonlocal started
            started += 1
            return ["1.1.1.1"]

        outcome: list[BaseException] = []

        def attempt() -> None:
            try:
                service._resolve_with_timeout(ninth, "ninth.example", timeout=0.1)
            except BaseException as exc:
                outcome.append(exc)

        waiter = threading.Thread(target=attempt)
        waiter.start()
        waiter.join(timeout=0.6)
        assert not waiter.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], TimeoutError)
        assert started == 0
    finally:
        release.set()
        for worker in holders:
            worker.join(timeout=2)


def test_hung_resolver_slot_reclaimed_after_hard_deadline() -> None:
    """永久挂死的解析过硬期限后归还闸位。硬期限不随 timeout 加长。

    八路假 resolver 等待永不 set 的 Event，注入 hard_deadline=0.05、timeout=2。
    硬期限已过而调用方尚未超时时，新调用必须已经能完成。不访问网络。
    """
    hung = threading.Event()
    entered = 0
    still_inside = 0
    lock = threading.Lock()
    all_entered = threading.Event()

    def never(host: str) -> list[str]:
        nonlocal entered, still_inside
        with lock:
            entered += 1
            still_inside += 1
            if entered >= 8:
                all_entered.set()
        try:
            hung.wait()
            return ["93.184.216.34"]
        finally:
            with lock:
                still_inside -= 1

    caller_errors: list[BaseException] = []

    def call_hung() -> None:
        try:
            service._resolve_with_timeout(
                never,
                "hung.example",
                timeout=2.0,
                hard_deadline=0.05,
            )
        except BaseException as exc:
            caller_errors.append(exc)

    callers = [threading.Thread(target=call_hung) for _ in range(8)]
    try:
        started_at = time.monotonic()
        for caller in callers:
            caller.start()
        assert all_entered.wait(timeout=1.0)
        # 只跨过注入的 0.05。此时仍在 2 秒调用预算内，闸位必须已经可还。
        opened = time.monotonic()
        while time.monotonic() - opened < 0.2:
            time.sleep(0.01)
        assert time.monotonic() - started_at < 0.5

        ran = 0

        def fast(host: str) -> list[str]:
            nonlocal ran
            ran += 1
            return ["93.184.216.34"]

        got = service._resolve_with_timeout(fast, "again.example", timeout=0.01)
        assert ran == 1
        assert [str(item) for item in got] == ["93.184.216.34"]

        for caller in callers:
            caller.join(timeout=3.0)
        assert all(not caller.is_alive() for caller in callers)
        assert len(caller_errors) == 8
        assert all(isinstance(exc, TimeoutError) for exc in caller_errors)
        assert all(str(exc) == "DNS resolution timed out" for exc in caller_errors)
    finally:
        hung.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with lock:
                if entered == 8 and still_inside == 0:
                    break
            time.sleep(0.01)
        for caller in callers:
            caller.join(timeout=0.2)


def test_non_positive_hard_deadline_raises_value_error() -> None:
    """注入 hard_deadline ≤ 0 时明确 ValueError，不占闸、不发起解析。"""

    def resolver(host: str) -> list[str]:
        raise AssertionError("不应发起解析")

    for bad in (0, 0.0, -0.01, -1):
        with pytest.raises(ValueError, match="hard_deadline must be > 0"):
            service._resolve_with_timeout(
                resolver,
                "bad.example",
                timeout=1.0,
                hard_deadline=bad,
            )
    _assert_gate_exactly_eight()


def test_normal_resolve_restores_gate_to_exactly_eight() -> None:
    """正常完成之后闸位恰为 8，不靠线程警告判断。"""

    def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    got = service._resolve_with_timeout(resolver, "ok.example", timeout=1.0)
    assert [str(item) for item in got] == ["93.184.216.34"]
    _assert_gate_exactly_eight()


def test_timeout_reclaim_restores_gate_to_exactly_eight() -> None:
    """超时回收之后闸位恰为 8，不靠线程警告判断。"""
    hung = threading.Event()
    entered = threading.Event()

    def never(host: str) -> list[str]:
        entered.set()
        hung.wait()
        return ["93.184.216.34"]

    def call_hung() -> None:
        try:
            service._resolve_with_timeout(
                never,
                "hung.example",
                timeout=2.0,
                hard_deadline=0.05,
            )
        except BaseException:
            pass

    caller = threading.Thread(target=call_hung)
    try:
        caller.start()
        assert entered.wait(timeout=1.0)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if service._DNS_RESOLUTION_GATE.acquire(timeout=0.05):
                service._DNS_RESOLUTION_GATE.release()
                break
        else:
            raise AssertionError("挂死线程的闸位没有在硬期限后归还")
        _assert_gate_exactly_eight()
    finally:
        hung.set()
        caller.join(timeout=3.0)


def _assert_gate_exactly_eight() -> None:
    """连续拿走 8 个闸位，第 9 次必须失败，再原样归还。"""
    gate = service._DNS_RESOLUTION_GATE
    capacity = service.MAX_CONCURRENT_DNS_RESOLUTIONS
    held = 0
    ninth = False
    try:
        for _ in range(capacity):
            assert gate.acquire(timeout=0.2)
            held += 1
        ninth = bool(gate.acquire(timeout=0.05))
        assert ninth is False
    finally:
        if ninth:
            gate.release()
        for _ in range(held):
            gate.release()
    assert held == capacity


def test_inflight_cap_refuses_new_resolve_and_returns_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """在飞数达到注入上限后，新调用立刻超时，闸位归还，线程数不再增加。"""
    assert service._RESOLVE_INFLIGHT_LIMIT == 32
    assert service._RESOLVE_ZOMBIE_BUDGET == 24
    before = service._resolve_inflight_count()
    cap = before + 2
    hung = threading.Event()
    entered = 0
    lock = threading.Lock()
    all_entered = threading.Event()

    def never(host: str) -> list[str]:
        nonlocal entered
        with lock:
            entered += 1
            if entered >= 2:
                all_entered.set()
        hung.wait()
        return ["93.184.216.34"]

    def call_hung() -> None:
        try:
            service._resolve_with_timeout(
                never,
                "hung.example",
                timeout=2.0,
                hard_deadline=0.05,
                inflight_limit=cap,
            )
        except BaseException:
            pass

    callers = [threading.Thread(target=call_hung) for _ in range(2)]
    try:
        for caller in callers:
            caller.start()
        assert all_entered.wait(timeout=1.0)
        free = time.monotonic() + 1.0
        while time.monotonic() < free:
            if service._DNS_RESOLUTION_GATE.acquire(timeout=0.05):
                service._DNS_RESOLUTION_GATE.release()
                break
        else:
            raise AssertionError("挂死线程的闸位没有在硬期限后归还")
        at_cap = service._resolve_inflight_count()
        assert at_cap == cap
        ran = 0

        def fast(host: str) -> list[str]:
            nonlocal ran
            ran += 1
            return ["1.1.1.1"]

        started = time.monotonic()
        with caplog.at_level("WARNING"):
            with pytest.raises(TimeoutError) as raised:
                service._resolve_with_timeout(
                    fast,
                    "again.example",
                    timeout=0.5,
                    hard_deadline=0.05,
                    inflight_limit=cap,
                )
        assert str(raised.value) == "DNS resolution timed out"
        assert time.monotonic() - started < 0.3
        assert ran == 0
        assert service._resolve_inflight_count() == at_cap
        assert any(
            "inflight cap" in record.message and str(cap) in record.message
            for record in caplog.records
        )
        _assert_gate_exactly_eight()
    finally:
        hung.set()
        for caller in callers:
            caller.join(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if service._resolve_inflight_count() == before:
                break
            time.sleep(0.01)
        assert service._resolve_inflight_count() == before


def test_thread_start_failure_returns_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """thread.start() 抛异常时闸位归还，在飞数不增加。"""

    def boom(thread: threading.Thread) -> None:
        raise RuntimeError("start failed")

    monkeypatch.setattr(service, "_start_resolve_thread", boom)
    before = service._resolve_inflight_count()

    def resolver(host: str) -> list[str]:
        raise AssertionError("不应发起解析")

    with pytest.raises(RuntimeError, match="start failed"):
        service._resolve_with_timeout(resolver, "start.example", timeout=1.0)
    assert service._resolve_inflight_count() == before
    _assert_gate_exactly_eight()


def test_timer_start_failure_returns_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timer 启动抛异常时，已经跑起来的挂死线程也不占着闸位。"""
    hung = threading.Event()

    def never(host: str) -> list[str]:
        hung.wait()
        return ["93.184.216.34"]

    def boom(timer: threading.Timer) -> None:
        raise RuntimeError("timer start failed")

    monkeypatch.setattr(service, "_start_resolve_timer", boom)
    before = service._resolve_inflight_count()
    try:
        with pytest.raises(RuntimeError, match="timer start failed"):
            service._resolve_with_timeout(never, "timer.example", timeout=2.0)
        _assert_gate_exactly_eight()
    finally:
        hung.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if service._resolve_inflight_count() == before:
                break
            time.sleep(0.01)
        assert service._resolve_inflight_count() == before

