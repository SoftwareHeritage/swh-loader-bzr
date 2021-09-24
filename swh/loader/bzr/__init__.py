# Copyright (C) 2021 The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information


from typing import Any, Mapping


def register() -> Mapping[str, Any]:
    from swh.loader.bzr.loader import BazaarLoader

    return {
        "task_modules": ["%s.tasks" % __name__],
        "loader": BazaarLoader,
    }
