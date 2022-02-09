# Copyright (C) 2022  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information


def test_loader(
    mocker, swh_config, swh_scheduler_celery_app, swh_scheduler_celery_worker
):
    mock_loader = mocker.patch("swh.loader.bzr.loader.BazaarLoader.load")
    mock_loader.return_value = {"status": "eventful"}

    res = swh_scheduler_celery_app.send_task(
        "swh.loader.bzr.tasks.LoadBazaar",
        kwargs={"url": "origin_url", "directory": "/some/repo", "visit_date": "now",},
    )

    assert res
    res.wait()
    assert res.successful()

    assert res.result == {"status": "eventful"}
    mock_loader.assert_called_once_with()
