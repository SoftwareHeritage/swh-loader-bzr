# Copyright (C) 2022-2023  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import uuid

import pytest

from swh.loader.tests import assert_module_tasks_are_scheduler_ready
from swh.scheduler.model import ListedOrigin, Lister

NAMESPACE = "swh.loader.bzr"


def test_tasks_loader_visit_type_match_task_name():
    import swh.loader.bzr

    assert_module_tasks_are_scheduler_ready([swh.loader.bzr])


@pytest.fixture
def bzr_lister():
    return Lister(name="bzr-lister", instance_name="example", id=uuid.uuid4())


@pytest.fixture
def bzr_listed_origin(bzr_lister):
    return ListedOrigin(
        lister_id=bzr_lister.id, url="https://bzr.example.org/repo", visit_type="bzr"
    )


@pytest.mark.parametrize(
    "extra_loader_arguments",
    [{"directory": "/some/repo"}, {"directory": "/some/repo", "visit_date": "now"}],
)
def test_loader_for_listed_origin(
    loading_task_creation_for_listed_origin_test,
    bzr_lister,
    bzr_listed_origin,
    extra_loader_arguments,
):
    bzr_listed_origin.extra_loader_arguments = extra_loader_arguments

    loading_task_creation_for_listed_origin_test(
        loader_class_name=f"{NAMESPACE}.loader.BazaarLoader",
        task_function_name=f"{NAMESPACE}.tasks.LoadBazaar",
        lister=bzr_lister,
        listed_origin=bzr_listed_origin,
    )
