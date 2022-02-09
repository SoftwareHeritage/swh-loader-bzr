# Copyright (C) 2022  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from typing import Optional

from celery import shared_task

from swh.loader.core.utils import parse_visit_date

from .loader import BazaarLoader


@shared_task(name=__name__ + ".LoadBazaar")
def load_bzr(
    *, url: str, directory: Optional[str] = None, visit_date: Optional[str] = None
):
    """Bazaar repository loading

    Args: see :func:`BazaarLoader` constructor.
    """
    loader = BazaarLoader.from_configfile(
        url=url, directory=directory, visit_date=parse_visit_date(visit_date)
    )
    return loader.load()
