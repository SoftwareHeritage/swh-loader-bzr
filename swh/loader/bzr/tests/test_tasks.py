# Copyright (C) 2022  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import uuid

import pytest

from swh.scheduler.model import ListedOrigin, Lister
from swh.scheduler.utils import create_origin_task_dict


@pytest.fixture(autouse=True)
def celery_worker_and_swh_config(swh_scheduler_celery_worker, swh_config):
    pass


@pytest.fixture
def bzr_lister():
    return Lister(name="bzr-lister", instance_name="example", id=uuid.uuid4())


@pytest.fixture
def bzr_listed_origin(bzr_lister):
    return ListedOrigin(
        lister_id=bzr_lister.id, url="https://bzr.example.org/repo", visit_type="bzr"
    )


def test_loader(
    mocker,
    swh_scheduler_celery_app,
):
    mock_loader = mocker.patch("swh.loader.bzr.loader.BazaarLoader.load")
    mock_loader.return_value = {"status": "eventful"}

    res = swh_scheduler_celery_app.send_task(
        "swh.loader.bzr.tasks.LoadBazaar",
        kwargs={
            "url": "origin_url",
            "directory": "/some/repo",
            "visit_date": "now",
        },
    )

    assert res
    res.wait()
    assert res.successful()

    assert res.result == {"status": "eventful"}
    mock_loader.assert_called_once_with()


def test_loader_for_listed_origin(
    mocker,
    swh_scheduler_celery_app,
    bzr_lister,
    bzr_listed_origin,
):
    mock_loader = mocker.patch("swh.loader.bzr.loader.BazaarLoader.load")
    mock_loader.return_value = {"status": "eventful"}

    task_dict = create_origin_task_dict(bzr_listed_origin, bzr_lister)

    res = swh_scheduler_celery_app.send_task(
        "swh.loader.bzr.tasks.LoadBazaar",
        kwargs=task_dict["arguments"]["kwargs"],
    )

    assert res
    res.wait()
    assert res.successful()

    assert res.result == {"status": "eventful"}
    mock_loader.assert_called_once_with()
