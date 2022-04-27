# Copyright (C) 2022  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from celery import shared_task

from swh.loader.core.utils import parse_visit_date

from .loader import BazaarLoader


def _process_kwargs(kwargs):
    if "visit_date" in kwargs:
        kwargs["visit_date"] = parse_visit_date(kwargs["visit_date"])
    return kwargs


@shared_task(name=__name__ + ".LoadBazaar")
def load_bzr(**kwargs):
    """Bazaar repository loading

    Args: see :func:`BazaarLoader` constructor.
    """
    loader = BazaarLoader.from_configfile(**_process_kwargs(kwargs))
    return loader.load()
