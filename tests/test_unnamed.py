"""B 节「未命名N」：默认命名、计数器持久、撞号顺延、自定义名不占号。

对应清单 B1–B6。链接路径在本批实现；录音路径（LZW-62）共用同一计数器，
本文件用 JobStore.allocate_unnamed_name 直接覆盖「录音侧也消费同一计数器」的口径。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app import service
from app.jobs import JobStore

_RESOLVER = MagicMock(return_value=["93.184.216.34"])


def _create_url_job(store: JobStore, root: Path, url: str = "https://example.com/a") -> str:
    status, body = service.create_url_job(store, root, url, "fast", resolver=_RESOLVER)
    assert status == 200, body
    return str(body["job_id"])


def _name(store: JobStore, job_id: str) -> str:
    job = store.get(job_id)
    assert job is not None
    return str(job["source_name"])


@pytest.fixture
def store_and_root(tmp_path: Path) -> tuple[JobStore, Path]:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    return JobStore(data / "jobs.db"), data


def test_b1_first_two_url_jobs_are_unnamed_1_and_2(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """B1：清空后首条链接任务标题「未命名1」，第二条「未命名2」，标题位不出现 http。"""
    store, root = store_and_root
    first = _create_url_job(store, root)
    second = _create_url_job(store, root, "https://www.youtube.com/watch?v=abc")

    first_name = _name(store, first)
    second_name = _name(store, second)
    assert first_name == "未命名1"
    assert second_name == "未命名2"
    for name in (first_name, second_name):
        assert "http" not in name
        assert "example.com" not in name
        assert "youtube" not in name


def test_b2_deleted_latest_number_is_not_reused(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """B2：删掉最新一条未命名任务后再建，序号不复用。"""
    store, root = store_and_root
    first = _create_url_job(store, root)
    second = _create_url_job(store, root)
    assert _name(store, second) == "未命名2"
    assert store.delete_job(second) is True

    third = _create_url_job(store, root)
    assert _name(store, third) == "未命名3"
    assert _name(store, first) == "未命名1"


def test_b3_collision_skips_to_next_free_number(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """B3：手工把某任务改成「未命名2」后新建，默认命名顺延到「未命名3」，不重名。"""
    store, root = store_and_root
    first = _create_url_job(store, root)
    assert store.rename_job(first, "未命名2") is True

    second = _create_url_job(store, root)
    name = _name(store, second)
    assert name == "未命名3"
    names = [str(job["source_name"]) for job in store.list_recent_jobs(100)]
    assert len(names) == len(set(names))


def test_b4_url_job_page_shows_domain_not_full_url(
    client: tuple, tmp_path: Path
) -> None:
    """B4：新建链接任务在列表与详情只露域名，标题位与页面正文都不出现完整 URL。"""
    http, data = client
    store = JobStore(data / "jobs.db")
    url = "https://www.bilibili.com/video/BV1xx411c7mD?p=1&token=SECRET_TOKEN_VALUE"
    status, body = service.create_url_job(store, data, url, "fast", resolver=_RESOLVER)
    assert status == 200, body
    job_id = str(body["job_id"])

    home = http.get("/")
    assert home.status_code == 200
    assert "bilibili.com" in home.text
    assert "https://www.bilibili.com" not in home.text
    assert "SECRET_TOKEN_VALUE" not in home.text

    detail = http.get(f"/jobs/{job_id}/view")
    assert detail.status_code == 200
    assert "bilibili.com" in detail.text
    assert "https://www.bilibili.com" not in detail.text
    assert "SECRET_TOKEN_VALUE" not in detail.text


def _default_named_job(store: JobStore) -> str:
    """走默认命名建一条任务（等价链接/录音未给名），返回任务 id。"""
    return store.create_job(
        tier="fast",
        source_name=None,
        source_kind="url",
        source_path=None,
        url="https://example.com/a",
    )


def test_b5_counter_survives_store_restart(tmp_path: Path) -> None:
    """B5：计数器落在库里，新开 JobStore（等价服务重启）后继续递增。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "jobs.db"

    first_store = JobStore(db)
    assert _name(first_store, _default_named_job(first_store)) == "未命名1"
    assert _name(first_store, _default_named_job(first_store)) == "未命名2"

    restarted = JobStore(db)
    assert _name(restarted, _default_named_job(restarted)) == "未命名3"


def test_f3_counter_starts_over_after_data_wipe(tmp_path: Path) -> None:
    """F3 前置：数据目录与任务库被清空后，首条默认命名回到「未命名1」。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "jobs.db"

    store = JobStore(db)
    assert _name(store, _default_named_job(store)) == "未命名1"
    assert _name(store, _default_named_job(store)) == "未命名2"
    del store

    # F 清空：删掉整个数据目录（含 jobs.db 与 counters 表），再按新库启动。
    shutil.rmtree(data)
    data.mkdir(parents=True, exist_ok=True)

    wiped = JobStore(data / "jobs.db")
    assert _name(wiped, _default_named_job(wiped)) == "未命名1"
    assert _name(wiped, _default_named_job(wiped)) == "未命名2"


def test_i1_peek_does_not_consume_a_number(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """I1：peek 只看不取号——反复预览后取号，序号从预览到的那个开始，不跳号。"""
    store, _root = store_and_root
    assert store.peek_unnamed_name() == "未命名1"
    assert store.peek_unnamed_name() == "未命名1"
    assert store.peek_unnamed_name() == "未命名1"

    assert _name(store, _default_named_job(store)) == "未命名1"
    assert store.peek_unnamed_name() == "未命名2"


def test_b6_custom_names_do_not_consume_numbers(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """B6（录音侧）：预填 peek 号后改成自定义名建录音，该号不被消耗，默认序号不跳。"""
    store, root = store_and_root
    first = _create_url_job(store, root)
    assert _name(store, first) == "未命名1"

    # 录音页预填「未命名2」：peek 不消耗
    prefilled = store.peek_unnamed_name()
    assert prefilled == "未命名2"

    # 用户改成自定义名结束录音：不占号
    custom_id = store.create_job(
        tier="fast",
        source_name="我的录音",
        source_kind="recording",
        source_path=None,
        url=None,
    )
    assert _name(store, custom_id) == "我的录音"

    # 之后贴链接默认命名，恰为预览到的「未命名2」（不跳号）
    second = _create_url_job(store, root)
    assert _name(store, second) == prefilled
    assert store.peek_unnamed_name() == "未命名3"


def test_b6_default_named_recording_consumes_its_own_number(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """B6 反向：录音用默认名结束（source_name=None）时确实占号，且号与预填一致。"""
    store, root = store_and_root
    prefilled = store.peek_unnamed_name()
    assert prefilled == "未命名1"

    recording_id = store.create_job(
        tier="fast",
        source_name=None,
        source_kind="recording",
        source_path=None,
        url=None,
    )
    assert _name(store, recording_id) == prefilled
    assert _name(store, _create_url_job(store, root)) == "未命名2"


def test_b_unnamed_prefix_is_never_a_url_shape(
    store_and_root: tuple[JobStore, Path],
) -> None:
    """补强：默认命名一律是「未命名N」形状，不含 scheme、host、query。"""
    store, root = store_and_root
    ids = [_create_url_job(store, root) for _ in range(3)]
    names = [_name(store, job_id) for job_id in ids]
    for name in names:
        assert re.fullmatch(r"未命名\d+", name), name


@pytest.mark.parametrize("count", [1, 2, 5])
def test_b_unnamed_numbers_are_monotonic(
    store_and_root: tuple[JobStore, Path], count: int
) -> None:
    """补强：连续取号严格单调递增，不复用。"""
    store, root = store_and_root
    ids = [_create_url_job(store, root) for _ in range(count)]
    numbers = [int(_name(store, job_id)[3:]) for job_id in ids]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == count
